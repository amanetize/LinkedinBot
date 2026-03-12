"""
scraper_job.py — Runs inside GitHub Actions.

Triggered by bot.py via repository_dispatch when user sends /start_cron.
Scrapes LinkedIn feed, evaluates posts with Groq, sends each target
directly to Telegram as an interactive card (same as before).

Flow:
  GitHub Actions starts this script
  → feed_reader.get_feed_posts() scrapes LinkedIn
  → each qualified post is sent to Telegram via bot API
  → bot.py (on PythonAnywhere) handles button presses as usual
"""

import asyncio
import os
import uuid

import requests
from dotenv import load_dotenv

from db import already_commented, log_target_created
from feed_reader import get_feed_posts

load_dotenv()

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TARGET_COUNT    = int(os.environ.get("TARGET_COUNT", "5"))

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def send_message(text: str, reply_markup: dict = None) -> dict:
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if reply_markup:
        import json
        payload["reply_markup"] = json.dumps(reply_markup)
    r = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=15)
    return r.json()


def notify(text: str):
    send_message(text)


async def main():
    notify(f"🔍 GitHub Actions: scanning LinkedIn for {TARGET_COUNT} targets...")

    found = 0

    async def on_target(data: dict):
        nonlocal found

        url = data.get("url", "")
        if already_commented(url):
            return

        found += 1
        target_id = str(uuid.uuid4())[:8]
        author    = data.get("author_name", "Unknown")
        title     = data.get("author_title", "")
        reason    = data.get("reason", "N/A")
        text      = data.get("text", "")
        raw_text  = data.get("raw_text", text)

        msg = (
            f"🎯 Target #{found}\n\n"
            f"👤 {author} — {title}\n\n"
            f"🔗 {url}\n\n"
            f"📝 {text[:250]}...\n\n"
            f"💡 Why: {reason}"
        )

        try:
            log_target_created(
                raw_text=raw_text,
                url=url,
                author_name=author,
                author_title=title,
                reason=reason,
                tele_msg=msg,
                target_id=target_id,
            )
        except Exception as e:
            print(f"[scraper] log error: {e}")

        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve_{target_id}"},
                {"text": "❌ Skip",    "callback_data": f"skip_{target_id}"},
            ]]
        }
        send_message(msg, reply_markup=keyboard)

    try:
        await get_feed_posts(on_target, max_targets=TARGET_COUNT)
    except Exception as e:
        notify(f"❌ Scraper error: {e}")
        raise

    notify(
        f"🏁 Scan complete! Found {found} targets.\n"
        f"Approve the ones you want to comment on."
    )


if __name__ == "__main__":
    asyncio.run(main())