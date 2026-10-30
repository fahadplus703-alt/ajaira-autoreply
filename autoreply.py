"""
Ajaira Gyan — auto comment replier for Facebook Page + Instagram.

Runs every ~10 minutes (GitHub Actions). Each run:
  1. Reads comments on recent posts (FB page + linked IG account).
  2. Skips comments the page already answered, spam/links, and the page's own comments.
  3. Writes a short, natural Bangla/Banglish reply with AI and posts it.

Everything is configured with environment variables (GitHub Secrets) — see README.md.
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------- config
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v26.0")
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

PAGE_ID = os.getenv("FB_PAGE_ID", "").strip()
PAGE_TOKEN = os.getenv("FB_PAGE_TOKEN", "").strip()
IG_USER_ID = os.getenv("IG_USER_ID", "").strip()          # optional: leave empty to skip Instagram

AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").lower()    # "gemini" (free tier) or "anthropic"
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))        # only posts from the last N days
MAX_POSTS = int(os.getenv("MAX_POSTS", "15"))               # per platform per run
MAX_REPLIES_PER_RUN = int(os.getenv("MAX_REPLIES_PER_RUN", "40"))
MIN_AGE_MINUTES = int(os.getenv("MIN_AGE_MINUTES", "0"))    # wait at least N min before replying
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"  # true = print replies, don't post

PAGE_STYLE = os.getenv("PAGE_STYLE", "")  # optional extra notes about the page voice

# ---------------------------------------------------------------- spam filter
SPAM_WORDS = [
    "dm me", "inbox me", "check my profile", "follow me", "follow back", "f4f", "l4l",
    "earn money", "income", "investment", "bitcoin", "crypto", "forex", "promo", "whatsapp",
    "telegram", "loan", "betting", "casino", "1xbet", "aviator",
]
LINK_RE = re.compile(r"(https?://|www\.|\.com\b|\.net\b|\.xyz\b|bit\.ly|t\.me/|wa\.me/)", re.I)


def is_spam(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    if LINK_RE.search(t):
        return True
    if any(w in t for w in SPAM_WORDS):
        return True
    if len(t) > 600:                      # walls of text are almost always spam/copy-paste
        return True
    if re.search(r"\d{9,}", t.replace(" ", "")):   # phone numbers
        return True
    return False


# ---------------------------------------------------------------- graph helpers
session = requests.Session()


def graph_get(path, **params):
    params["access_token"] = PAGE_TOKEN
    r = session.get(f"{GRAPH}/{path}", params=params, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"GET {path}: {data['error'].get('message')}")
    return data


def graph_post(path, **params):
    params["access_token"] = PAGE_TOKEN
    r = session.post(f"{GRAPH}/{path}", data=params, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"POST {path}: {data['error'].get('message')}")
    return data


def parse_time(ts: str) -> datetime:
    # Graph returns e.g. 2026-10-30T15:04:05+0000
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")


def old_enough(ts: str) -> bool:
    return datetime.now(timezone.utc) - parse_time(ts) >= timedelta(minutes=MIN_AGE_MINUTES)


# ---------------------------------------------------------------- AI reply
SYSTEM_PROMPT = f"""You reply to comments on "Ajaira Gyan" (আজাইরা জ্ঞান), a Bangladeshi Facebook/Instagram
page that posts short reels: fun "জানেন কি?" facts, motivation, and tips on relationships, mind and money.

How to reply:
- Write like a friendly, witty Bangladeshi page admin. Warm, a little playful, never robotic.
- Match the commenter's language: Bangla script -> Bangla; Banglish (Bangla in English letters) -> Banglish;
  English -> simple English.
- ONE short sentence, max ~20 words. At most one emoji. No hashtags, no links.
- React to what they actually said. If they ask a question about the video's topic, answer briefly and correctly;
  if you aren't sure, say you'll cover it in a future video instead of guessing.
- If they just tag a friend or drop an emoji, reply with something light and fun.
- Never argue, never promise prizes/money/services, never share personal info, never mention you are an AI.
- If the comment is abusive, hateful, political, sexual, religious debate, or anything risky to answer,
  reply with exactly: SKIP
{PAGE_STYLE}
Output only the reply text."""


def ai_reply(comment: str, post_caption: str) -> str | None:
    user_msg = f"Post caption: {post_caption[:500] or '(no caption)'}\n\nComment: {comment}"
    if AI_PROVIDER == "anthropic":
        r = session.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": ANTHROPIC_MODEL, "max_tokens": 150, "system": SYSTEM_PROMPT,
                  "messages": [{"role": "user", "content": user_msg}]},
            timeout=60,
        )
        data = r.json()
        if "content" not in data:
            raise RuntimeError(f"Anthropic error: {data}")
        text = "".join(b.get("text", "") for b in data["content"])
    else:
        r = session.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY, "content-type": "application/json"},
            json={"systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                  "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
                  "generationConfig": {"maxOutputTokens": 150, "temperature": 0.9}},
            timeout=60,
        )
        data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            raise RuntimeError(f"Gemini error: {data}")

    text = text.strip().strip('"').strip()
    if not text or text.upper().startswith("SKIP"):
        return None
    return text[:300]


# ---------------------------------------------------------------- Facebook
def facebook_jobs(since: datetime):
    """Yield (comment_id, comment_text, caption) for FB comments the page hasn't answered."""
    posts = graph_get(f"{PAGE_ID}/posts", fields="id,message,created_time", limit=MAX_POSTS)
    for post in posts.get("data", []):
        if parse_time(post["created_time"]) < since:
            continue
        caption = post.get("message", "")
        comments = graph_get(
            f"{post['id']}/comments",
            fields="id,message,created_time,from{id},comments.limit(25){from{id}}",
            filter="toplevel", order="reverse_chronological", limit=100,
        )
        for c in comments.get("data", []):
            if (c.get("from") or {}).get("id") == PAGE_ID:
                continue  # our own comment
            replies = (c.get("comments") or {}).get("data", [])
            if any((x.get("from") or {}).get("id") == PAGE_ID for x in replies):
                continue  # already answered
            if not old_enough(c["created_time"]):
                continue
            yield ("fb", c["id"], c.get("message", ""), caption)


def facebook_reply(comment_id, text):
    graph_post(f"{comment_id}/comments", message=text)


# ---------------------------------------------------------------- Instagram
def instagram_jobs(since: datetime):
    me = graph_get(IG_USER_ID, fields="username")
    my_name = me.get("username", "").lower()
    media = graph_get(f"{IG_USER_ID}/media", fields="id,caption,timestamp", limit=MAX_POSTS)
    for m in media.get("data", []):
        if parse_time(m["timestamp"]) < since:
            continue
        caption = m.get("caption", "")
        comments = graph_get(
            f"{m['id']}/comments",
            fields="id,text,username,timestamp,replies{username}", limit=100,
        )
        for c in comments.get("data", []):
            if (c.get("username") or "").lower() == my_name:
                continue
            replies = (c.get("replies") or {}).get("data", [])
            if any((x.get("username") or "").lower() == my_name for x in replies):
                continue
            if not old_enough(c["timestamp"]):
                continue
            yield ("ig", c["id"], c.get("text", ""), caption)


def instagram_reply(comment_id, text):
    graph_post(f"{comment_id}/replies", message=text)


# ---------------------------------------------------------------- main
def main():
    missing = [n for n, v in [("FB_PAGE_ID", PAGE_ID), ("FB_PAGE_TOKEN", PAGE_TOKEN)] if not v]
    if AI_PROVIDER == "anthropic" and not ANTHROPIC_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if AI_PROVIDER != "anthropic" and not GEMINI_KEY:
        missing.append("GEMINI_API_KEY")
    if missing:
        sys.exit(f"Missing settings: {', '.join(missing)} (add them as GitHub Secrets)")

    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    sources = [("Facebook", facebook_jobs, facebook_reply)]
    if IG_USER_ID:
        sources.append(("Instagram", instagram_jobs, instagram_reply))

    done = skipped = errors = 0
    for name, jobs, reply in sources:
        try:
            for platform, cid, text, caption in jobs(since):
                if done >= MAX_REPLIES_PER_RUN:
                    print(f"Reached MAX_REPLIES_PER_RUN={MAX_REPLIES_PER_RUN}, rest next run.")
                    break
                if is_spam(text):
                    skipped += 1
                    print(f"[{platform}] skip spam: {text[:60]!r}")
                    continue
                try:
                    answer = ai_reply(text, caption)
                    if not answer:
                        skipped += 1
                        print(f"[{platform}] AI skipped: {text[:60]!r}")
                        continue
                    if DRY_RUN:
                        print(f"[{platform}] DRY  {text[:60]!r} -> {answer}")
                    else:
                        reply(cid, answer)
                        print(f"[{platform}] sent {text[:60]!r} -> {answer}")
                    done += 1
                    time.sleep(2)  # be gentle with Meta's rate limits
                except Exception as e:  # one bad comment shouldn't stop the run
                    errors += 1
                    print(f"[{platform}] error on {cid}: {e}")
        except Exception as e:
            errors += 1
            print(f"{name}: {e}")

    print(f"\nDone. replied={done} skipped={skipped} errors={errors} dry_run={DRY_RUN}")
    if errors and not done:
        sys.exit(1)  # makes the GitHub run show red so you notice (e.g. expired token)


if __name__ == "__main__":
    main()
