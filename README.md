# LinkedIn Networking Bot 🚀

Automated LinkedIn engagement bot controlled via Telegram. Scan qualified posts, approve targets, auto-post AI-drafted comments, and share curated AI news—all from Telegram, 24/7.

## Features

✨ **Smart Target Scanning** — AI evaluates LinkedIn posts and sends high-value targets to Telegram with interactive review cards
✨ **AI Comment Drafting** — Context-aware comment generation using latest AI (Llama 3.1)
✨ **Comment Customization** — Regenerate, write custom comment, or rephrase with your instruction
✨ **News Generation** — Top-5 AI news weekly with Tavily search + AI drafting
✨ **Activity Logging** — Full audit trail: every scrape, draft, action stored in MongoDB
✨ **Koyeb deployment** — long-lived Telegram bot on Koyeb with scan + posting
✨ **GitHub Actions** — scheduled Playwright script runs for targeted tasks
✨ **MongoDB storage** — shared state and audit logs
✨ **UptimeRobot** — keeps Koyeb bot alive with periodic pings

## Quick Start

### 1. Local Setup (Test First)

```bash
# Clone and setup
git clone <your-repo>
cd linkedin_bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium

# Configure environment
cp .env.example .env
nano .env  # fill in all values
```

### 2. Fill in .env

```
LI_EMAIL=your_linkedin_email@gmail.com
LI_PASSWORD=your_linkedin_password
TELEGRAM_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=987654321
GROQ_API_KEY=gsk_xxxxxxxxxxxxx
TAVILY_API_KEY=tvly-xxxxxxxxxxxxx
MONGO_URI=mongodb+srv://username:password@cluster.mongodb.net/linkedin_bot?retryWrites=true&w=majority
```

See [SETUP.md](SETUP.md#2-configure-environment) for details on getting each value.

### 3. Run Locally

```bash
python3 bot.py
```

The bot will start listening. Test it:
- Send `/start_cron` on Telegram
- Approve a target
- Watch it draft and post comments

### 4. Deploy Stack (Live)

This repository is designed for:
- **Koyeb**: runs `bot.py` as a persistent Telegram bot (scanning + posting)
- **GitHub Actions**: runs Playwright scripts (scheduled cron jobs) for scraping/maintenance
- **MongoDB**: shared state, deduplication, rate limiting, audit logs
- **UptimeRobot**: periodic ping to bot endpoint to keep process awake on Koyeb

Example flow:
1. On Koyeb, deploy at least one service running `python3 bot.py`.
2. Set environment vars (Koyeb secrets): `TELEGRAM_TOKEN`, `LI_EMAIL`, `LI_PASSWORD`, `GROQ_API_KEY`, `TAVILY_API_KEY`, `MONGO_URI`, etc.
3. Use GitHub Actions for scheduled job e.g. nightly Playwright page checks:
   - `.github/workflows/playwright.yml` runs `python playwright_script.py`
   - results stored in MongoDB; optionally trigger bot actions.
4. Configure UptimeRobot to hit an HTTP health endpoint on Koyeb every 5 minutes.

No Netlify is needed in this architecture.

## Commands

| Command | What it does |
|---------|-------------|
| `/start_cron` | Scan LinkedIn feed for qualified posts (1–20 targets) |
| `/post_news` | Generate top-5 AI news post for LinkedIn |
| `/stop` | Emergency stop (cancels scan, clears queue) |
| `/cancel` | Cancel current operation |

## Workflow

### Targets (LinkedIn Posts)

1. `/start_cron` → bot asks how many targets
2. Each target arrives on Telegram with:
   - 👤 Author name & title
   - 🔗 Post URL
   - 📝 Post snippet
   - 💡 Why this target
3. **Buttons** (choose one):
   - ✅ Post → AI draft queued for posting
   - ❌ Drop → discard
   - 🔄 Regenerate → new draft (same post)
   - 📝 Custom → type your own comment
   - ✏️ Rephrase → rewrite with your instruction
4. Background worker posts every 5–10 min with live Telegram updates

### News Posts

1. `/post_news` → Tavily searches top AI news (past week)
2. AI creates 5 bullet points
3. **Buttons**:
   - ✅ Post to LinkedIn → publish
   - ❌ Drop → discard
   - ✏️ Rephrase → rewrite with instruction
   - 🔃 Fetch again → new search + new draft

## Architecture

```
bot.py              Telegram listener + background worker
feed_reader.py      LinkedIn feed scanner (Playwright)
ai.py               Comment/news drafting (Groq Llama)
poster.py           LinkedIn post writer (Playwright)
db.py               MongoDB activity logging + dedup
li_cookies.json     LinkedIn session (auto-refreshed)
```

## Safety & Limits

- **AI Model**: `meta-llama/llama-3.1-8b-instant` (via Groq)
- **Daily limit**: 10 comments/day (hard-coded, enforced via MongoDB)
- **Delay**: 300–600s random between posts
- **Deduplication**: No duplicate posts commented on
- **Activity logging**: Full audit trail in MongoDB

## Free Tier APIs

| Service | Free Tier | Link |
|---------|-----------|------|
| Telegram | Unlimited | [@BotFather](https://t.me/BotFather) |
| Groq | 30 req/min | [console.groq.com](https://console.groq.com/keys) |
| Tavily | 100 searches/month | [app.tavily.com](https://app.tavily.com) |
| MongoDB | M0 (512MB) | [mongodb.com/cloud/atlas](https://www.mongodb.com/cloud/atlas) |
| Koyeb | 2 services + 24/7 | [koyeb.com](https://koyeb.com) |

**Total cost: $0** (unless you exceed free tier quotas)

## Database Schema

See [COLLECTIONS.md](COLLECTIONS.md) for full MongoDB schema:
- `commented_posts` — deduplication
- `daily_count` — 10/day limit
- `warm_leads` — weekly review list
- `activity_logs` — full audit trail (raw posts, Telegram msgs, drafts, actions)

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Bot not responding | Check `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` |
| MongoDB connection fails | Verify `MONGO_URI` and whitelist your hosting IP(s) (0.0.0.0/0 for testing) |
| LinkedIn login fails | Disable 2FA on LinkedIn account |
| "Can't parse entities" | All messages use plain text; this error shouldn't occur |

See [SETUP.md § 11](SETUP.md#11-troubleshooting) for more troubleshooting.

## Development

### File Structure

```
.
├── bot.py                # Telegram listener + worker
├── feed_reader.py        # LinkedIn feed scanner
├── ai.py                 # AI generation (comments, news)
├── poster.py             # LinkedIn poster
├── db.py                 # MongoDB helpers
├── requirements.txt      # Python dependencies
├── runtime.txt           # Python version (Koyeb)
├── Procfile              # Koyeb run command
├── .env.example          # Template for environment vars
├── .gitignore            # Git exclusions
├── SETUP.md              # Setup & deployment guide
├── COLLECTIONS.md        # MongoDB schema reference
└── README.md             # This file
```

### Local Development

```bash
# Activate venv
source venv/bin/activate

# Run bot
python3 bot.py

# To stop: Ctrl+C
```

## Contributing

Submit issues or PRs to improve the bot.

## License

[Your license here]

---

**Questions?** See [SETUP.md](SETUP.md) for detailed setup and deployment instructions.

**Deploy now**: [Koyeb + GitHub Actions Setup](SETUP.md#9-production-deployment-koyeb--github-actions--uptimerobot)
