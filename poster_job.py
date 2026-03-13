"""
poster_job.py — Runs inside GitHub Actions.

Reads post_id from env, fetches url+comment from MongoDB,
posts the comment via Playwright, sends Telegram result.
"""

import asyncio, os, requests
from dotenv import load_dotenv
from db import (
    get_pending_post, mark_commented, increment_today_count,
    save_warm_lead, log_target_action, log_target_final,
)
from poster import post_comment

load_dotenv()

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
POST_ID          = os.environ["POST_ID"]
API_BASE         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def send_message(text: str):
    requests.post(f"{API_BASE}/sendMessage", json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text":    text,
    }, timeout=15)


async def main():
    data = get_pending_post(POST_ID)
    if not data:
        send_message(f"❌ poster_job: no pending post found for id {POST_ID}")
        return

    url         = data["url"]
    comment     = data["comment"]
    author_name = data.get("author_name", "Unknown")
    log_id      = data.get("log_id")

    send_message(f"⏳ Posting comment on {author_name}'s post...")

    success = await post_comment(url, comment)

    if success:
        mark_commented(url)
        increment_today_count()
        if log_id:
            try:
                log_target_action(log_id, "posted")
                log_target_final(log_id, "posted", comment)
            except Exception as e:
                print(f"[poster_job] log error: {e}")
        save_warm_lead(
            author_name=author_name,
            author_title=data.get("author_title", ""),
            post_snippet=data.get("text", "")[:300],
            comment=comment,
        )
        send_message(f"✅ Comment posted on {author_name}'s post!")
    else:
        send_message(f"❌ Failed to post comment on {author_name}'s post.")


if __name__ == "__main__":
    asyncio.run(main())
