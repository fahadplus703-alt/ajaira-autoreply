# Ajaira Gyan — Auto Comment Reply (Facebook + Instagram)

Every ~10 minutes, this checks comments on your last 7 days of posts and replies with a short,
natural Bangla/Banglish line written by AI. It skips spam/links, abusive comments, your own comments,
and anything the page already replied to — so nobody gets two replies.

Runs free on GitHub Actions (your PC can be off). Total setup: ~25 minutes, once.

---

## Step 1 — Instagram must be linked to the Facebook page
@ajaira.gyan must be a **Business or Creator** account and linked to the Ajaira Gyan Facebook page
(Instagram → Settings → Account type and tools / Accounts Centre). You've already done this.

## Step 2 — Create a Meta app (~5 min)
1. Go to **developers.facebook.com** → log in with the Facebook account that is admin of the page.
2. **My Apps → Create App** → use case: **"Other"** → type **"Business"** → name it `Ajaira Autoreply`.
3. Leave the app in **Development mode**. That's fine — it can still manage pages *you* admin, so no App Review is needed.

## Step 3 — Get a page token that never expires (~10 min)
1. Open **Tools → Graph API Explorer**. Pick your app at top-right.
2. Under **Permissions**, add:
   `pages_show_list`, `pages_read_engagement`, `pages_read_user_content`, `pages_manage_engagement`,
   `instagram_basic`, `instagram_manage_comments`, `business_management`
3. Click **Generate Access Token** → approve → select the Ajaira Gyan page and @ajaira.gyan.
4. Make it long-lived: open **Tools → Access Token Debugger**, paste the token, click **Debug**, then
   **Extend Access Token** at the bottom. Copy the new (long-lived) token.
5. Back in Graph API Explorer, paste that long-lived token in the token box and run:
   `GET  me/accounts?fields=id,name,access_token,instagram_business_account`
6. From the result, copy:
   - `id` → your **FB_PAGE_ID**
   - `access_token` → your **FB_PAGE_TOKEN** (this page token does not expire — check in the Debugger: "Expires: Never")
   - `instagram_business_account.id` → your **IG_USER_ID**

⚠️ Treat the page token like a password. Don't share it in chats or screenshots.

## Step 4 — Get a free AI key (~2 min)
- Go to **aistudio.google.com** → **Get API key** → create key. That's your **GEMINI_API_KEY**.
  The free tier easily covers a page this size.
- (Optional, better replies, paid) Use a Claude key from **console.anthropic.com** as **ANTHROPIC_API_KEY**
  and set the variable `AI_PROVIDER` = `anthropic` in Step 5.

## Step 5 — Put it on GitHub (~5 min)
1. Create a free account at **github.com** → **New repository** → name `ajaira-autoreply` →
   **Private** → Create.
2. **Add file → Upload files** → drag in everything from this folder (including the `.github` folder —
   if your file explorer hides it, upload `autoreply.yml` into the path `.github/workflows/`).
3. Repo **Settings → Secrets and variables → Actions → New repository secret**, add:
   | Name | Value |
   |---|---|
   | `FB_PAGE_ID` | from Step 3 |
   | `FB_PAGE_TOKEN` | from Step 3 |
   | `IG_USER_ID` | from Step 3 |
   | `GEMINI_API_KEY` | from Step 4 |
4. Same page → **Variables** tab → add `DRY_RUN` = `true` for the first test.

## Step 6 — Test, then go live
1. Repo → **Actions** tab → enable workflows if asked → **Ajaira Gyan auto-reply → Run workflow**.
2. Open the run → see lines like `DRY 'সত্যি নাকি?' -> হ্যাঁ, একদম সত্যি! 🐙`. Nothing is posted in dry-run.
3. Happy with the replies? Change `DRY_RUN` to `false`. From now it runs every 10 minutes by itself.

---

## Settings you can change (Variables tab)
| Variable | Default | What it does |
|---|---|---|
| `DRY_RUN` | `false` | `true` = show replies in the log without posting |
| `MIN_AGE_MINUTES` | `0` | Wait at least this long after a comment before replying (e.g. `5` feels more human) |
| `AI_PROVIDER` | `gemini` | `gemini` (free) or `anthropic` |

Reply tone lives in `SYSTEM_PROMPT` inside `autoreply.py` — edit it any time.

## Good to know
- **Timing:** GitHub's 10-minute schedule can slip by a few minutes at busy hours, so replies land
  roughly 5–20 minutes after a comment.
- **Free limits:** a private repo gets 2,000 free Actions minutes/month; each run takes well under a
  minute, which fits. If it ever runs short, make the repo **public** (secrets stay hidden) for unlimited minutes.
- **If replies stop:** the Actions run turns red. Usually the token was invalidated (e.g. you changed your
  Facebook password) — redo Step 3 and update `FB_PAGE_TOKEN`.
- **If GitHub turns the schedule off** after 60 days with no repo changes, just click "Enable workflow" again.
