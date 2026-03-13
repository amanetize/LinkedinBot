# LinkedIn Bot — Setup Guide

## 1. Install Python + dependencies

```bash
# On Ubuntu/Oracle Cloud VM:
sudo apt update && sudo apt install python3-pip python3-venv -y

# Create and activate venv
python3 -m venv venv
source venv/bin/activate

# Install everything
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium
```

## 2. Configure environment

```bash
cp .env.example .env
nano .env          # fill in all values
```

### How to get each value:

| Variable | Where to get it |
|----------|------------------|
| **LI_EMAIL / LI_PASSWORD** | Your LinkedIn login credentials (2FA should be disabled) |
| **TELEGRAM_TOKEN** | [Telegram](https://telegram.org) → search **@BotFather** → `/newbot` → follow steps → copy token |
| **TELEGRAM_CHAT_ID** | Open Telegram → search **@userinfobot** → it replies with your numeric chat ID |
| **GROQ_API_KEY** | [console.groq.com](https://console.groq.com/keys) → Create key (free tier: 30 req/min) |
| **TAVILY_API_KEY** | [app.tavily.com](https://app.tavily.com) → sign up → API key (free tier: 100 searches/month) |
| **MONGO_URI** | [MongoDB Atlas](https://www.mongodb.com/cloud/atlas) → Cluster → Connect → Drivers → copy connection string (free M0 tier) |

## 3. Start the Bot

```bash
python3 bot.py
```

The bot starts listening for Telegram commands. It stays alive until you kill the process.

## 4. Telegram Commands & Features

### Core Commands

| Command | What it does |
|---------|-------------|
| `/start_cron` | Scan LinkedIn feed for qualified posts. Bot asks how many targets (1–20), then sends each as an interactive card. |
| `/post_news` | Generate a top-5 AI news post. Buttons: Post, Rephrase, Fetch again, Drop. |
| `/stop` | Emergency stop. Cancels scan, clears queue, resets all state. |
| `/cancel` | Cancel current operation (rephrase flow, custom comment, etc.). |

### Target (LinkedIn Post) Workflow

When you approve a target, you get:

```
🎯 Target #1

👤 Author Name — Title

🔗 https://www.linkedin.com/posts/...

📝 Post snippet (cleaned)

💡 Why: [AI reason]
```

**Buttons:**
- **Row 1:** ✅ Post | ❌ Drop
- **Row 2:** 🔄 Regenerate | 📝 Custom | ✏️ Rephrase

**Actions:**
- **✅ Post** — Post this comment to LinkedIn (queued for background posting)
- **❌ Drop** — Discard this comment
- **🔄 Regenerate** — AI generates a new comment from scratch (same post context)
- **📝 Custom** — Type your own comment (bot waits for next message)
- **✏️ Rephrase** — Provide an instruction; AI rewrites the current draft (e.g. "make it shorter", "more formal")

### News Workflow

When you run `/post_news`, you get a top-5 AI news post:

```
📰 Draft LinkedIn Post:

• News story 1
• News story 2
• News story 3
• News story 4
• News story 5

#ai #artificialintelligence #machinelearning
```

**Buttons:**
- **Row 1:** ✅ Post to LinkedIn | ❌ Drop
- **Row 2:** ✏️ Rephrase | 🔃 Fetch again

**Actions:**
- **✅ Post to LinkedIn** — Post this draft to your LinkedIn
- **❌ Drop** — Discard this draft
- **✏️ Rephrase** — Provide an instruction; AI rephrases the same 5 news items (e.g. "make it more casual", "focus on funding")
- **🔃 Fetch again** — New Tavily search + new top-5 news + new draft

## 5. File Structure

```
linkedin_bot/
├── bot.py              # Main entry: Telegram listener + background worker
├── feed_reader.py      # Async LinkedIn feed scanner (Playwright + AI evaluation)
├── ai.py               # AI comment/news drafting (Llama + Tavily)
├── poster.py           # Async LinkedIn poster (Playwright)
├── db.py               # MongoDB activity logging & deduplication
├── li_cookies.json     # Persistent LinkedIn session (auto-refreshed)
├── requirements.txt    # Python dependencies
├── SETUP.md            # This file
├── COLLECTIONS.md      # MongoDB collections schema
└── .env                # Secrets (never commit!)
```

## 6. How It Works

### Full Flow

1. **You send `/start_cron`** → bot asks target count (1–20)
2. **Live streaming** — each high-value post arrives as an interactive card with buttons
3. **You approve post** → bot scrapes existing comments + AI drafts your comment → shows for review
4. **You confirm/edit** (Post, Regenerate, Custom, Rephrase, or Drop)
5. **Background worker** posts approved comments one-by-one with 5–10 min delays
6. **You get live updates** ("✅ Comment 2/10 posted!")
7. **Session handling** — if LinkedIn cookies expire, bot auto-re-logs in

### For News Posts

1. **You send `/post_news`** → Tavily searches top AI news from past week (5 results)
2. **AI creates 5 bullet points** from the search results
3. **You review/edit** (Post, Rephrase, Fetch again, or Drop)
4. **You post to LinkedIn** → appears on your profile

## 7. Safety & Limits

* **AI Model**: Uses `meta-llama/llama-3.1-8b-instant` (via Groq) for comments and news rephrase
* **Search**: Tavily for web search (100 searches/month free tier)
* **Daily limit**: Hard-coded 10 comments/day maximum (enforced via MongoDB)
* **Delay**: 300–600s randomized delay between posted comments
* **Deduplication**: MongoDB prevents commenting on the same post twice
* **Activity logging**: Every action (approve, post, rephrase, drop, etc.) is logged with full history

## 8. Database Collections

All data is stored in MongoDB database `linkedin_bot`. See [COLLECTIONS.md](COLLECTIONS.md) for full schema.

**Collections:**

| Collection | Purpose |
|-----------|----------|
| `commented_posts` | Deduplication (URL → timestamp) |
| `daily_count` | 10 comments/day limit enforcement |
| `warm_leads` | Weekly review: who you commented to |
| `activity_logs` | **Full audit trail**: raw scraped posts, Telegram messages, all versions, actions, final outcome |

**Activity Logs** store:
- **Target logs**: raw post, URL, author, why selected, Telegram message, all comment versions, each action (approve/skip/post/drop), final comment
- **News logs**: raw Tavily search, all drafts (fetch/rephrase), Telegram messages, actions, final posted content

## 9. Production Deployment (Koyeb + GitHub Actions + UptimeRobot)

Your production stack is:
- **Koyeb**: runs the Telegram bot process (`python3 bot.py`) continuously for scanning + posting.
- **GitHub Actions**: runs Playwright scripts as cron jobs for supplemental scraping or maintenance tasks.
- **MongoDB**: shared state and audit log, deduplication and daily limits.
- **UptimeRobot**: pings the Koyeb bot endpoint every 5 minutes to maintain session uptime.

### Quick Start: Koyeb Bot

1. Create a service on Koyeb, repository connection to this project.
2. Set environment variables in Koyeb Dashboard:
   - `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
   - `LI_EMAIL`, `LI_PASSWORD`
   - `GROQ_API_KEY`, `TAVILY_API_KEY`
   - `MONGO_URI`
3. Use launch command: `python3 bot.py`.
4. Ensure `runtime.txt` or `Dockerfile` uses Python 3.11+.
5. Deploy and verify in Koyeb logs: `🤖 Bot is running. Commands: /start_cron, /post_news, /stop`.

### GitHub Actions (Playwright schedule)

Create `.github/workflows/playwright.yml` with schedule (e.g., daily):

```yaml
name: Playwright maintenance
on:
  schedule:
    - cron: '0 3 * * *' # daily at 03:00 UTC
jobs:
  run-playwright:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: playwright install --with-deps chromium
      - run: python path/to/playwright_script.py
        env:
          MONGO_URI: ${{ secrets.MONGO_URI }}
          LI_EMAIL: ${{ secrets.LI_EMAIL }}
          LI_PASSWORD: ${{ secrets.LI_PASSWORD }}
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
```

### UptimeRobot

1. Sign up at [uptimerobot.com](https://uptimerobot.com).
2. Create HTTP monitor:
   - URL: `https://<koyeb-service-url>/` (or a health endpoint implemented in `bot.py` if needed).
   - Interval: 5 min.
3. If monitor detects downtime, it will alert and restart your bot automatically.

### Test Your Deployment

- `/start_cron` from Telegram should initiate scan.
- `/post_news` should produce a draft news item.
- Bot should log actions in MongoDB.

## 10. Running Locally

For testing before deploying to your production host:

```bash
# 1. Activate venv
source venv/bin/activate

# 2. Make sure .env is filled
cat .env

# 3. Run bot locally
python3 bot.py

# Bot will listen and respond to Telegram commands
# To stop: Ctrl+C
```

## 11. Troubleshooting

### "Can't parse entities" Telegram error
**Fix**: All bot messages now use plain text (no Markdown). This error should not occur. If it does, check that `parse_mode` is not being set in `bot.py`.

### LinkedIn login fails
- Check credentials in `.env`
- Disable 2FA on your LinkedIn account temporarily (bot can't handle 2FA challenges)
- If LinkedIn suddenly blocks you, wait 24h; LinkedIn rate-limits aggressive bots

### No MongoDB connection
- Check `MONGO_URI` is correct (copy from Atlas → Connect → Drivers)
- Ensure IP is whitelisted in Atlas (or set to 0.0.0.0/0 for testing only)
- If using Koyeb MongoDB, use Koyeb's internal connection string

### Groq/Tavily API rate limits
- **Groq**: Free tier has 30 requests/minute; comments + news rephrase should be fine
- **Tavily**: 100 searches/month free tier; if you hit this, pay for more or wait until next month

## 12. Next Steps

1. Install dependencies: `pip install -r requirements.txt && playwright install chromium`
2. Fill in `.env` with all API keys and credentials
3. Start locally: `python3 bot.py` — test all commands
4. Deploy to Koyeb (see section 9)
5. Run `/start_cron` on Telegram → approve targets → watch background posting
6. Check `activity_logs` in MongoDB for full audit trail

**Happy networking!** 🚀