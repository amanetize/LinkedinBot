"""
poster_job.py — Runs inside GitHub Actions.

Reads post_id from env, fetches url+comment from MongoDB,
posts the comment via Playwright, edits the original Telegram
message to show final status.
"""

import asyncio, os, json, requests
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


def edit_message(message_id: int, text: str):
    """Edit the original target card message."""
    if not message_id:
        return
    requests.post(f"{API_BASE}/editMessageText", json={
        "chat_id":    TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text":       text,
    }, timeout=15)


def _build_target_card(d: dict, status: str, comment: str = None) -> str:
    author   = d.get("author_name", "Unknown")
    title    = d.get("author_title", "")
    conn     = d.get("connection_level", "")
    text     = (d.get("text") or "")[:300]
    likes    = d.get("likes_count", 0)
    comments = d.get("comments_count", 0)
    idx      = d.get("target_index", "")

    parts = [f"🎯 Target #{idx}" if idx else "🎯 Target", ""]
    parts += [f"👤 {author}", f"💼 {title}"]
    if conn:
        parts.append(f"🔗 {conn} connection")
    parts += ["", f"📝 {text}...", ""]
    if likes or comments:
        parts.append(f"❤️ {likes} likes  💬 {comments} comments")
    parts += ["", f"📊 Status: {status}"]
    if comment:
        parts += ["", "🗨️ Your comment:", comment]
    return "\n".join(parts)


async def main():
    data = get_pending_post(POST_ID)
    if not data:
        send_message(f"❌ poster_job: no pending post found for id {POST_ID}")
        return

    url         = data["url"]
    comment     = data["comment"]
    author_name = data.get("author_name", "Unknown")
    log_id      = data.get("log_id")
    message_id  = data.get("message_id")

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
        # Edit original card to show final status
        final_text = _build_target_card(data, "✅ Posted", comment)
        edit_message(message_id, final_text)
    else:
        final_text = _build_target_card(data, "❌ Post failed", comment)
        edit_message(message_id, final_text)
        send_message(f"❌ Failed to post comment on {author_name}'s post.")


if __name__ == "__main__":
    asyncio.run(main())
