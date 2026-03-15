"""
scraper_job.py — Runs inside GitHub Actions.

Scrapes LinkedIn, saves each target to MongoDB (pending_targets),
sends the Telegram card + screenshot photo, then deletes the temp file.
bot.py reads from MongoDB on Approve.
"""

import asyncio, os, uuid, requests
from pathlib import Path
from dotenv import load_dotenv
from db import already_commented, log_target_created, save_pending_target
from poster import scrape_comments
from feed_reader import get_feed_posts

load_dotenv()

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TARGET_COUNT     = int(os.environ.get("TARGET_COUNT", "5"))
API_BASE         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def send_message(text: str, reply_markup: dict = None) -> dict:
    import json
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    r = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=15)
    return r.json()


def send_photo(photo_path: str) -> None:
    """Send a photo message (visual context only, no buttons)."""
    with open(photo_path, "rb") as f:
        requests.post(
            f"{API_BASE}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID},
            files={"photo": f},
            timeout=30,
        )


async def main():
    send_message(f"🔍 GitHub Actions: scanning LinkedIn for {TARGET_COUNT} targets...")
    found = 0

    async def on_target(data: dict):
        nonlocal found
        url = data.get("url", "")
        if already_commented(url):
            return

        found            += 1
        target_id         = str(uuid.uuid4())[:8]
        author            = data.get("author_name", "Unknown")
        title             = data.get("author_title", "")
        reason            = data.get("reason", "N/A")
        text              = data.get("text", "")
        raw_text          = data.get("raw_text", text)
        connection_level  = data.get("connection_level", "")
        likes_count       = data.get("likes_count", 0)
        comments_count    = data.get("comments_count", 0)
        screenshot_path   = data.get("screenshot_path")   # temp file from feed_reader

        conn_str   = f"🔗 {connection_level} connection" if connection_level else ""
        counts_str = f"❤️ {likes_count} likes  💬 {comments_count} comments" if (likes_count or comments_count) else ""

        # Build card text (also used as photo caption)
        msg_parts = [f"🎯 Target #{found}", ""]
        msg_parts += [f"👤 {author}", f"💼 {title}"]
        if conn_str:   msg_parts.append(conn_str)
        msg_parts += ["", f"📝 {text[:300]}...", ""]
        if counts_str: msg_parts.append(counts_str)
        msg_parts += ["", f"📊 Status: ⏳ Pending", f"💡 Why: {reason}"]
        msg_parts += ["", f"🔗 {url}"]
        card_text = "\n".join(msg_parts)

        # Scrape existing comments (GitHub Actions IP is trusted)
        existing_comments = []
        try:
            existing_comments = await scrape_comments(url)
            print(f"[scraper] Scraped {len(existing_comments)} comments for {author}")
        except Exception as e:
            print(f"[scraper] comment scrape error: {e}")

        # Persist to MongoDB
        log_id = None
        try:
            log_id = log_target_created(
                raw_text=raw_text, url=url, author_name=author,
                author_title=title, reason=reason, target_id=target_id,
            )
        except Exception as e:
            print(f"[scraper] log_target_created error: {e}")

        try:
            save_pending_target(
                target_id=target_id, url=url, text=text,
                author_name=author, author_title=title,
                reason=reason, log_id=log_id,
                existing_comments=existing_comments,
                connection_level=connection_level,
                likes_count=likes_count,
                comments_count=comments_count,
                target_index=found,
            )
        except Exception as e:
            print(f"[scraper] save_pending_target error: {e}")

        keyboard = {"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve_{target_id}"},
            {"text": "❌ Skip",    "callback_data": f"skip_{target_id}"},
        ]]}

        # Photo is sent first for visual context (no buttons on it).
        # Buttons live on the text card below so edit_message_text works in bot.py.
        if screenshot_path and Path(screenshot_path).exists():
            try:
                send_photo(screenshot_path)
            except Exception as e:
                print(f"[scraper] send_photo error: {e}")
            finally:
                try:
                    Path(screenshot_path).unlink(missing_ok=True)
                    print(f"[scraper] Deleted temp screenshot for {author}")
                except Exception:
                    pass

        # Text card with Approve/Skip buttons — message_id saved for later edits
        result     = send_message(card_text, reply_markup=keyboard)
        message_id = result.get("result", {}).get("message_id")

        # Save message_id so bot.py / poster_job.py can edit the card later
        if message_id:
            try:
                from db import get_db
                get_db().pending_targets.update_one(
                    {"target_id": target_id},
                    {"$set": {"message_id": message_id}}
                )
            except Exception as e:
                print(f"[scraper] message_id save error: {e}")

    try:
        await get_feed_posts(on_target, max_targets=TARGET_COUNT)
    except Exception as e:
        send_message(f"❌ Scraper error: {e}")
        raise

    send_message(
        f"🏁 Scan complete! Found {found} targets.\n"
        f"Approve the ones you want to comment on."
    )


if __name__ == "__main__":
    asyncio.run(main())