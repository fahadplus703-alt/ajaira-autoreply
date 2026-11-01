"""
Auto comment replier for Fahad's Facebook Pages + Instagram accounts
(Ajaira Gyan, Mon Mechanic, Doodle Logic).

Each run:
  1. For every configured page, reads comments on recent FB posts AND FB reels,
     and on recent Instagram posts/reels of the linked IG account.
  2. Skips comments the page already answered, spam/links, and the page's own comments.
  3. Writes a short, natural reply with AI (retries + fallback models) and posts it.
  4. On scheduled runs it keeps polling every POLL_SECONDS for RUN_MINUTES, so replies
     land within a few minutes even when GitHub starts the workflow late.
  5. Turns the run RED if a comment is still unanswered because of an error after
     ALERT_AFTER_MINUTES, or if a page/account can't be read — so misses never stay silent.

Configure with GitHub Secrets — see README.md. Per page, secrets use a prefix:
  Ajaira Gyan  : FB_PAGE_ID,    FB_PAGE_TOKEN,    IG_USER_ID      (no prefix, original setup)
  Mon Mechanic : MM_FB_PAGE_ID, MM_FB_PAGE_TOKEN, MM_IG_USER_ID
  Doodle Logic : DL_FB_PAGE_ID, DL_FB_PAGE_TOKEN, DL_IG_USER_ID
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

AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").lower()    # "gemini" (free tier) or "anthropic"
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip() or "gemini-2.5-flash"
GEMINI_FALLBACKS = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACK_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m.strip()]
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))        # only posts from the last N days
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "60"))               # max posts/reels checked per source
MAX_REPLIES_PER_PASS = int(os.getenv("MAX_REPLIES_PER_RUN", "40"))
MIN_AGE_MINUTES = int(os.getenv("MIN_AGE_MINUTES", "0") or 0)
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
RUN_MINUTES = float(os.getenv("RUN_MINUTES", "0") or 0)      # >0 = keep polling this long
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "150"))
ALERT_AFTER_MINUTES = int(os.getenv("ALERT_AFTER_MINUTES", "30"))

PAGES = [
    {
        "key": "AG", "prefix": "", "name": "Ajaira Gyan (আজাইরা জ্ঞান)",
        "about": 'a Bangladeshi page that posts short reels and posts: fun "জানেন কি?" facts, '
                 "word/proverb stories, myths vs facts, nostalgia and everyday tricks",
        "lang": "Bangla",
    },
    {
        "key": "MM", "prefix": "MM_", "name": "Mon Mechanic (মন মেকানিক)",
        "about": "a Bangladeshi page that posts short reels: practical tips on relationships, "
                 "the mind/psychology and money (কাজের কথা), and motivation (Level Up)",
        "lang": "Bangla",
    },
    {
        "key": "DL", "prefix": "DL_", "name": "Doodle Logic",
        "about": "a global English page that posts funny 2D animated 'how things work' explainer reels "
                 "with a mascot called Pip",
        "lang": "English",
    },
]

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
    if re.search(r"\d{9,}", t.replace(" ", "")):   # phone numbers
        return True
    return False


# ---------------------------------------------------------------- graph helpers
session = requests.Session()


def graph_get(token, path, **params):
    params["access_token"] = token
    r = session.get(f"{GRAPH}/{path}", params=params, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"GET {path}: {data['error'].get('message')}")
    return data


def graph_post(token, path, **params):
    params["access_token"] = token
    r = session.post(f"{GRAPH}/{path}", data=params, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"POST {path}: {data['error'].get('message')}")
    return data


def graph_list(token, path, max_items, stop=None, **params):
    """Follow `paging.next` until max_items, no more pages, or stop(item) is True."""
    data = graph_get(token, path, **params)
    count = 0
    while True:
        for item in data.get("data", []):
            if stop and stop(item):
                return
            yield item
            count += 1
            if count >= max_items:
                return
        nxt = (data.get("paging") or {}).get("next")
        if not nxt:
            return
        r = session.get(nxt, timeout=30)
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"GET {path} (next page): {data['error'].get('message')}")


def parse_time(ts: str) -> datetime:
    # Graph returns e.g. 2026-10-30T15:04:05+0000
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")


def age_minutes(ts: str) -> float:
    return (datetime.now(timezone.utc) - parse_time(ts)).total_seconds() / 60


# ---------------------------------------------------------------- AI reply
def system_prompt(page) -> str:
    return f"""You reply to comments on "{page['name']}", {page['about']}.

How to reply:
- Write like a friendly, witty page admin. Warm, a little playful, never robotic.
- Match the commenter's language: Bangla script -> Bangla; Banglish (Bangla in English letters) -> Banglish;
  English -> simple English. The page's main language is {page['lang']}.
- ONE short sentence, max ~20 words. At most one emoji. No hashtags, no links.
- React to what they actually said. If they ask a question about the post's topic, answer briefly and correctly;
  if you aren't sure, say you'll cover it in a future video instead of guessing.
- If they just tag a friend, drop an emoji or a number, reply with something light and fun.
- Never argue, never promise prizes/money/services, never share personal info, never mention you are an AI.
- If the comment is abusive, hateful, political, sexual, religious debate, or anything risky to answer,
  reply with exactly: SKIP
{os.getenv(page['prefix'] + 'PAGE_STYLE', '')}
Output only the reply text."""


class AIError(RuntimeError):
    pass


def _gemini(model, sys_prompt, user_msg):
    r = session.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": GEMINI_KEY, "content-type": "application/json"},
        json={"systemInstruction": {"parts": [{"text": sys_prompt}]},
              "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
              "generationConfig": {"maxOutputTokens": 150, "temperature": 0.9}},
        timeout=60,
    )
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise AIError(f"Gemini {model}: {str(data)[:300]}")


def _anthropic(sys_prompt, user_msg):
    r = session.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": ANTHROPIC_MODEL, "max_tokens": 150, "system": sys_prompt,
              "messages": [{"role": "user", "content": user_msg}]},
        timeout=60,
    )
    data = r.json()
    if "content" not in data:
        raise AIError(f"Anthropic: {str(data)[:300]}")
    return "".join(b.get("text", "") for b in data["content"])


def ai_reply(page, comment: str, post_caption: str) -> str | None:
    sys_prompt = system_prompt(page)
    user_msg = f"Post caption: {post_caption[:500] or '(no caption)'}\n\nComment: {comment}"

    # Order of attempts: chosen provider first, then fallbacks. Each gets a retry for
    # temporary overloads (503 / 429), which is what made a run fail on 1 Nov 2026.
    attempts = []
    if AI_PROVIDER == "anthropic" and ANTHROPIC_KEY:
        attempts.append(("anthropic", None))
    if GEMINI_KEY:
        for m in [GEMINI_MODEL] + GEMINI_FALLBACKS:
            if ("gemini", m) not in attempts:
                attempts.append(("gemini", m))
    if AI_PROVIDER != "anthropic" and ANTHROPIC_KEY:
        attempts.append(("anthropic", None))

    last = None
    for provider, model in attempts:
        for wait in (0, 8):
            if wait:
                time.sleep(wait)
            try:
                text = _anthropic(sys_prompt, user_msg) if provider == "anthropic" \
                    else _gemini(model, sys_prompt, user_msg)
                text = text.strip().strip('"').strip()
                if not text or text.upper().startswith("SKIP"):
                    return None
                return text[:300]
            except (AIError, requests.RequestException, ValueError) as e:
                last = e
                print(f"   AI attempt failed ({provider} {model or ''}): {str(e)[:160]}")
    raise AIError(f"all AI attempts failed; last: {last}")


# ---------------------------------------------------------------- Facebook
def facebook_jobs(page, since):
    """Yield (platform, comment_id, text, caption, created) for unanswered FB comments
    on recent posts AND reels (reels don't show up in /posts)."""
    pid, token = page["fb_id"], page["fb_token"]
    old = lambda item: parse_time(item["created_time"]) < since  # noqa: E731

    objects = []
    for p in graph_list(token, f"{pid}/posts", MAX_ITEMS, stop=old,
                        fields="id,message,created_time", limit=50):
        objects.append((p["id"], p.get("message", "")))
    n_posts = len(objects)
    for v in graph_list(token, f"{pid}/video_reels", MAX_ITEMS, stop=old,
                        fields="id,description,created_time", limit=50):
        objects.append((v["id"], v.get("description", "")))
    page["stats"].append(f"FB posts={n_posts} reels={len(objects) - n_posts}")

    seen = set()
    for obj_id, caption in objects:
        for c in graph_list(token, f"{obj_id}/comments", 500,
                            fields="id,message,created_time,from{id},comments.limit(50){from{id}}",
                            filter="toplevel", order="reverse_chronological", limit=100):
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            if (c.get("from") or {}).get("id") == pid:
                continue  # our own comment
            replies = (c.get("comments") or {}).get("data", [])
            if any((x.get("from") or {}).get("id") == pid for x in replies):
                continue  # already answered
            yield ("fb", c["id"], c.get("message", ""), caption, c["created_time"])


def facebook_reply(page, comment_id, text):
    graph_post(page["fb_token"], f"{comment_id}/comments", message=text)


# ---------------------------------------------------------------- Instagram
def instagram_jobs(page, since):
    ig, token = page["ig_id"], page["fb_token"]
    me = graph_get(token, ig, fields="username")
    my_name = me.get("username", "").lower()
    old = lambda m: parse_time(m["timestamp"]) < since  # noqa: E731
    media = list(graph_list(token, f"{ig}/media", MAX_ITEMS, stop=old,
                            fields="id,caption,timestamp", limit=50))
    page["stats"].append(f"IG @{my_name} media={len(media)}")
    for m in media:
        caption = m.get("caption", "")
        for c in graph_list(token, f"{m['id']}/comments", 500,
                            fields="id,text,username,timestamp,replies{username}", limit=100):
            if (c.get("username") or "").lower() == my_name:
                continue
            replies = (c.get("replies") or {}).get("data", [])
            if any((x.get("username") or "").lower() == my_name for x in replies):
                continue
            yield ("ig", c["id"], c.get("text", ""), caption, c["timestamp"])


def instagram_reply(page, comment_id, text):
    graph_post(page["fb_token"], f"{comment_id}/replies", message=text)


# ---------------------------------------------------------------- main
def discover_pages(tokens):
    """If a saved token is a USER token (not a page token), list every page it manages,
    with each page's own token and linked Instagram account. Returns {name_lower: (id, token, ig)}."""
    found = {}
    for tok in tokens:
        try:
            for a in graph_list(tok, "me/accounts", 100,
                                fields="id,name,access_token,instagram_business_account", limit=100):
                ig = (a.get("instagram_business_account") or {}).get("id", "")
                found[a.get("name", "").lower()] = (a["id"], a.get("access_token", ""), ig)
        except Exception as e:
            print(f"   (auto-detect: this token can't list pages — {str(e)[:120]})")
    if found:
        print("   auto-detected pages: " + ", ".join(found))
    return found


def match_discovered(page, found):
    keys = {"AG": ["ajaira", "আজাইরা"], "MM": ["mon mechanic", "মন মেকানিক", "mechanic"],
            "DL": ["doodle logic", "doodle"]}[page["key"]]
    for name, val in found.items():
        if any(k in name for k in keys) and val[1]:
            return val
    return None


def configured_pages():
    pages, warnings = [], []
    found = None
    for p in PAGES:
        if not os.getenv(p["prefix"] + "FB_PAGE_TOKEN", "").strip():
            if found is None:
                found = discover_pages({os.getenv(q["prefix"] + "FB_PAGE_TOKEN", "").strip()
                                        for q in PAGES} - {""})
            hit = match_discovered(p, found)
            if hit:
                pages.append({**p, "fb_id": hit[0], "fb_token": hit[1], "ig_id": hit[2]})
                if not hit[2]:
                    warnings.append(f"{p['name']}: no Instagram account linked to this Facebook page")
                continue
        fb_id = os.getenv(p["prefix"] + "FB_PAGE_ID", "").strip()
        fb_token = os.getenv(p["prefix"] + "FB_PAGE_TOKEN", "").strip()
        ig_id = os.getenv(p["prefix"] + "IG_USER_ID", "").strip()
        if not (fb_id and fb_token):
            warnings.append(f"{p['name']} is NOT connected — add secrets "
                            f"{p['prefix']}FB_PAGE_ID, {p['prefix']}FB_PAGE_TOKEN, {p['prefix']}IG_USER_ID")
            continue
        if not ig_id:
            warnings.append(f"{p['name']}: Instagram NOT connected — add secret {p['prefix']}IG_USER_ID")
        pages.append({**p, "fb_id": fb_id, "fb_token": fb_token, "ig_id": ig_id})
    return pages, warnings


def one_pass(pages, failed):
    """One sweep over all pages. `failed` maps comment_id -> (page, platform, created, error)
    for comments that are still unanswered because something went wrong."""
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    done = skipped = 0
    source_errors = []
    for page in pages:
        page["stats"] = []
        sources = [("Facebook", facebook_jobs, facebook_reply)]
        if page["ig_id"]:
            sources.append(("Instagram", instagram_jobs, instagram_reply))
        for name, jobs, reply in sources:
            try:
                for platform, cid, text, caption, created in jobs(page, since):
                    if done >= MAX_REPLIES_PER_PASS:
                        print("Reached MAX_REPLIES_PER_RUN, rest next pass.")
                        break
                    if age_minutes(created) < MIN_AGE_MINUTES:
                        continue
                    tag = f"[{page['key']} {platform}]"
                    if is_spam(text):
                        skipped += 1
                        failed.pop(cid, None)
                        print(f"{tag} skip spam: {text[:60]!r}")
                        continue
                    try:
                        answer = ai_reply(page, text, caption)
                        if not answer:
                            skipped += 1
                            failed.pop(cid, None)
                            print(f"{tag} AI skipped: {text[:60]!r}")
                            continue
                        if DRY_RUN:
                            print(f"{tag} DRY  {text[:60]!r} -> {answer}")
                        else:
                            reply(page, cid, answer)
                            print(f"{tag} sent {text[:60]!r} -> {answer}")
                        failed.pop(cid, None)
                        done += 1
                        time.sleep(2)  # be gentle with Meta's rate limits
                    except Exception as e:  # one bad comment shouldn't stop the run
                        failed[cid] = (page["name"], platform, created, str(e)[:300])
                        print(f"{tag} error on {cid}: {e}")
            except Exception as e:
                source_errors.append(f"{page['name']} {name}: {e}")
                print(f"!! {page['name']} {name}: {e}")
        print(f"   {page['key']}: " + ", ".join(page["stats"]))
    return done, skipped, source_errors


def main():
    if not GEMINI_KEY and not ANTHROPIC_KEY:
        sys.exit("Missing settings: GEMINI_API_KEY or ANTHROPIC_API_KEY (add as GitHub Secret)")
    pages, warnings = configured_pages()
    for w in warnings:
        print(f"::warning::{w}")
    if not pages:
        sys.exit("No page is configured — add FB_PAGE_ID and FB_PAGE_TOKEN secrets.")

    failed = {}
    total_done = total_skipped = 0
    deadline = time.time() + RUN_MINUTES * 60
    passes = 0
    while True:
        passes += 1
        print(f"\n--- pass {passes} at {datetime.now(timezone.utc):%H:%M:%S} UTC")
        done, skipped, source_errors = one_pass(pages, failed)
        total_done += done
        total_skipped += skipped
        if time.time() + POLL_SECONDS > deadline:
            break
        time.sleep(POLL_SECONDS)

    print(f"\nDone. passes={passes} replied={total_done} skipped={total_skipped} "
          f"dry_run={DRY_RUN}")

    # Alarm: anything we could not read, or comments still unanswered because of errors.
    problems = list(source_errors)
    for cid, (pname, platform, created, err) in failed.items():
        if age_minutes(created) >= ALERT_AFTER_MINUTES or not RUN_MINUTES:
            problems.append(f"{pname} {platform} comment {cid} unanswered: {err}")
    for p in problems:
        print(f"::error::{p}")
    if problems:
        sys.exit(1)  # red run -> GitHub emails the owner


if __name__ == "__main__":
    main()
