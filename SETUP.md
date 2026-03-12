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

## 9. Free Deployment on Koyeb (24/7, No CC Required)

**Koyeb is ideal for this bot**: free tier includes 2 services, 24/7 uptime, no credit card, and perfect for long-running Telegram bots.

### Quick Start: Koyeb Deployment

**Prerequisites:**
- GitHub account with this repo pushed
- Koyeb account (free, no CC): [koyeb.com](https://www.koyeb.com)
- MongoDB Atlas (free tier M0 cluster): [mongodb.com/cloud/atlas](https://www.mongodb.com/cloud/atlas)

**Steps:**

#### 1. Create MongoDB on Atlas (if not done)

```bash
# Go to MongoDB Atlas
# → Create free M0 shared cluster
# → Set network access to 0.0.0.0/0 (allows any IP)
# → Create database user (username/password)
# → Copy connection string:
# mongodb+srv://username:password@cluster.mongodb.net/linkedin_bot?retryWrites=true&w=majority
```

#### 2. Deploy to Koyeb

1. **Sign up** at [koyeb.com](https://www.koyeb.com) (GitHub login works)
2. **Create a new app**:
   - GitHub → select your repo → main branch
   - Runtime: Python
   - Launch command: `python3 bot.py`
   - Name your service (e.g., `linkedin-bot`)
   - Keep instance size: Free (512MB RAM)
3. **Add environment variables** before deploying:
   - Click "Environment" tab (or during creation, scroll to "Other Options")
   - Add all variables from `.env`:

```
LI_EMAIL=your_linkedin_email@gmail.com
LI_PASSWORD=your_linkedin_password
TELEGRAM_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=987654321
GROQ_API_KEY=gsk_xxxxxxxxxxxxx
TAVILY_API_KEY=tvly-xxxxxxxxxxxxx
MONGO_URI=mongodb+srv://username:password@cluster.mongodb.net/linkedin_bot?retryWrites=true&w=majority
```

4. **Deploy**: Click "Create" → Koyeb builds and deploys automatically
5. **Check logs**: Koyeb dashboard → your service → Logs → should see bot starting

#### 3. Test It

Send any message on Telegram to your bot:
- It should respond (if bot is running on Koyeb)
- Send `/start_cron` and approve a target
- Watch it post in the background

#### 4. Keep Running

When your Koyeb free instance redeploys or restarts, the bot automatically reconnects to Telegram. **No manual restart needed**—just set and forget.

### Koyeb Free Tier Limits

| Resource | Free Tier | Need More? |
|----------|-----------|------------|
| Services | 2 | Paid starts $3/service/month |
| Compute | 2 vCPU, 4GB RAM total | Enough for this bot |
| Data transfer | 100GB/month | Plenty for Telegram + LinkedIn |
| Uptime | 24/7 SLA | No sleep, no suspend |
| Builds | Unlimited | Auto-deploy on push |
| Credit card | **Not required** | Free tier doesn't charge |

### Koyeb vs. Alternatives

| Hosting | Free Tier | No CC | 24/7 Uptime | Setup Complexity |
|---------|-----------|-------|-------------|------------------|
| **Koyeb** | ✅ 2 services | ✅ Yes | ✅ Yes | ⭐ Very easy |
| Railway | ✅ $5/mo credit | ⚠️ Needs card after | ✅ Yes | ⭐ Easy |
| Render | ✅ Limited | ✅ Yes | ⚠️ 15min timeout on web | ⭐⭐ Medium |
| Oracle Cloud | ✅ Always Free | ⚠️ Billing info | ✅ Yes | ⭐⭐⭐ Hard |

### Troubleshooting Koyeb

**Bot not responding on Telegram:**
- Check Koyeb logs: Service → Logs
- Confirm `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` are set correctly
- Try restart: Service → Restart

**MongoDB connection fails:**
- Verify `MONGO_URI` in Koyeb environment variables
- In MongoDB Atlas: Network Access → check IP is whitelisted (0.0.0.0/0 for testing)
- Test locally first to confirm MONGO_URI is correct

**Playwright fails (LinkedIn login errors):**
- Koyeb runs headless Chromium fine; rare issue
- If stuck: wait 5 min, then restart service

**Out of free tier resources:**
- Koyeb: 2 services free; if you need more, upgrade to paid or create a new free account
- Bot itself uses ~300MB RAM; well within free tier

### After Deployment

Once on Koyeb:
- You never need to keep your laptop running
- `/start_cron` and `/post_news` work anywhere, anytime
- All comments post automatically with delays
- MongoDB logs everything in `activity_logs` collection

**That's it!** 🚀 Your bot is now live 24/7 on Koyeb.

## 10. Running Locally

For testing before deploying to Koyeb:

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