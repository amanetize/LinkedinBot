# Koyeb Deployment Guide

This guide walks you through deploying the LinkedIn Bot to Koyeb for **free, 24/7 uptime with no credit card required**.

## Why Koyeb?

✅ **Free tier**: 2 services + 24/7 uptime  
✅ **No credit card**: Completely free, no charges  
✅ **Auto-deploy**: Push to GitHub → auto-builds and deploys  
✅ **Perfect for bots**: Long-running processes stay alive  
✅ **Built-in logs**: Real-time debugging from dashboard  

## Prerequisites

1. **GitHub account** with the bot repo
2. **Koyeb account** (free signup): [koyeb.com](https://koyeb.com)
3. **MongoDB Atlas** (free M0 cluster): [mongodb.com/cloud/atlas](https://www.mongodb.com/cloud/atlas)
4. **API keys** ready:
   - LinkedIn credentials (2FA disabled)
   - Telegram token & chat ID
   - Groq API key
   - Tavily API key

## Step 1: Set Up MongoDB Atlas

### 1.1 Create a free cluster

- Go to [mongodb.com/cloud/atlas](https://www.mongodb.com/cloud/atlas)
- Sign up or log in
- Click "Create" → "M0 (Sandbox)" → select region → Create cluster
- Wait ~3 minutes for cluster to be ready

### 1.2 Create database credentials

- Cluster → "Security" tab → "Database Access"
- Click "Add New Database User"
- Username: `linkedin_bot`
- Password: (use a strong one, you'll copy it)
- Role: `readWriteAnyDatabase`
- Click "Add Users"

### 1.3 Get the connection string

- Cluster → "Connect" button
- Select "Drivers"
- Copy the connection string (looks like):
  ```
  mongodb+srv://linkedin_bot:PASSWORD@cluster0.mongodb.net/linkedin_bot?retryWrites=true&w=majority
  ```
- Replace `<password>` with the password you set
- Replace `localhost/linkedin_bot` part with `/linkedin_bot` (database name)

### 1.4 Allow Koyeb IP access

- Cluster → "Security" tab → "Network Access"
- Click "Add IP Address"
- Click "Allow Access from Anywhere" (sets to `0.0.0.0/0`)
- Confirm

**Save your connection string** — you'll need it for Koyeb.

## Step 2: Prepare Your GitHub Repo

### 2.1 Push to GitHub

Make sure you've:
- Created a GitHub repo
- Pushed the bot code
- Ensure `.gitignore` excludes `.env` and `li_cookies.json`

**Check that these files are in your repo:**
```
✓ Procfile              (tells Koyeb how to run)
✓ runtime.txt           (Python version)
✓ requirements.txt      (dependencies)
✓ .env.example          (template)
✓ bot.py, feed_reader.py, ai.py, etc.  (code)
```

### 2.2 Verify `.env` is NOT committed

```bash
git status  # Should NOT show .env
git rm --cached .env  # If it does, remove it
```

## Step 3: Deploy to Koyeb

### 3.1 Sign up at Koyeb

- Go to [koyeb.com](https://koyeb.com)
- Click "Sign up free"
- Use GitHub login (faster) or email

### 3.2 Connect GitHub

- Koyeb dashboard → "Create a new app"
- Click "GitHub" → "Connect GitHub"
- Authorize Koyeb
- Select your repo → main branch

### 3.3 Configure deployment

1. **Fill in basic info:**
   - Service name: `linkedin-bot`
   - Instance type: Free
   - Runtime: Python (auto-detected)

2. **Click "Environment" tab:**
   - Add all variables from `.env.example`:
   
   ```
   LI_EMAIL=your_email@gmail.com
   LI_PASSWORD=your_password
   TELEGRAM_TOKEN=123456:ABC-DEF...
   TELEGRAM_CHAT_ID=987654321
   GROQ_API_KEY=gsk_...
   TAVILY_API_KEY=tvly_...
   MONGO_URI=mongodb+srv://linkedin_bot:PASSWORD@cluster0...
   ```

3. **Deploy:**
   - Click "Create service"
   - Wait for build (2–3 minutes)
   - Check "Logs" tab to confirm bot started

### 3.4 Test it works

Send a message to your Telegram bot:
```
/start_cron
```

It should ask you how many targets (1–20). If you get a response, it's working! 🎉

## Troubleshooting on Koyeb

### Bot not responding on Telegram

1. **Check environment variables:**
   - Koyeb dashboard → Service → Settings
   - Verify `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` are set

2. **Check logs:**
   - Koyeb dashboard → Service → Logs
   - Look for errors or "Bot is running" message

3. **Restart service:**
   - Settings → Restart

### MongoDB connection fails

**Error:** `Could not connect to MongoDB`

1. **Verify `MONGO_URI`:**
   - Should be in format: `mongodb+srv://user:password@cluster.mongodb.net/linkedin_bot?retryWrites=true&w=majority`
   - Check username/password are correct
   - Password should NOT have `@` (if it does, URL-encode it as `%40`)

2. **Check IP whitelist:**
   - MongoDB Atlas → Security → Network Access
   - Should show 0.0.0.0/0 (Anywhere) as allowed

3. **Test locally first:**
   ```bash
   python3 -c "from pymongo import MongoClient; MongoClient('YOUR_MONGO_URI')"
   ```
   - If this works locally, it should work on Koyeb

### LinkedIn login fails

**Error:** `Could not log in to LinkedIn`

1. **Disable 2FA:**
   - Go to LinkedIn → Settings → Sign in & security
   - Turn off 2-Step verification
   - Bot can't handle 2FA prompts

2. **Check credentials:**
   - Verify `LI_EMAIL` and `LI_PASSWORD` are correct in Koyeb environment

3. **Verify account isn't locked:**
   - Try logging in manually on LinkedIn
   - If locked, unlock it and wait 24h before retrying via bot

### Out of free tier

**Koyeb free tier includes:**
- 2 services (you have 1 bot = OK)
- 2 vCPU, 4GB RAM total
- 100GB data transfer

If you get this error, you're good—the bot uses ~300MB RAM.

## Keeping Your Bot Up to Date

When you make changes locally:

```bash
# Make changes
nano bot.py  # or any file

# Push to GitHub (Koyeb auto-deploys!)
git add .
git commit -m "Update bot"
git push origin main
```

Koyeb automatically rebuilds and redeploys within 2–3 minutes.

## Koyeb Free Tier Limits

| Resource | Limit | Enough? |
|----------|-------|---------|
| Services | 2 | ✅ (you use 1) |
| Compute | 2 vCPU, 4GB RAM | ✅ (bot uses ~300MB) |
| Data transfer | 100GB/month | ✅ (Telegram + LinkedIn << 100GB) |
| Build uploads | 2GB total | ✅ (your repo ~100MB) |
| Uptime | 24/7 SLA | ✅ No sleep, no throttling |

## What Happens on Koyeb Restart?

When Koyeb restarts your service (rare, ~monthly maintenance):

1. **Bot re-connects to Telegram** ✓ (automatic)
2. **LinkedIn cookies are recreated** ✓ (one login, then works)
3. **MongoDB connection restored** ✓ (Atlas handles it)
4. **All pending tasks resume** ✓ (state stored in MongoDB)

**Total downtime:** <30 seconds. No action needed on your part.

## Monitoring

### Check bot status

```bash
# From your local terminal
curl https://YOUR_KOYEB_URL  # If you add a web endpoint (optional)
```

**Or** just send a Telegram message and check if it responds.

### View logs in real-time

1. Koyeb dashboard → Your service
2. "Logs" tab
3. Logs appear in real-time as bot runs

## Next Steps

1. ✅ Deployed to Koyeb
2. 🔄 Test: Send `/start_cron` on Telegram
3. 📊 Monitor: Check Koyeb logs dashboard
4. 🔧 Update: Make changes locally, push to GitHub (auto-deploys)

## Support

See [SETUP.md](../SETUP.md) for detailed configuration and troubleshooting.

---

**Your bot is now live 24/7!** 🚀
