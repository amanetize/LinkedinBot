"""
bot.py — LinkedIn Networking Bot (Telegram-controlled)

Commands:
  /start_cron       — Ask how many targets, then scan feed
  /post_news        — Generate an AI news post for LinkedIn
  /stop             — Cancel everything

KOYEB FIX: Runs a tiny aiohttp health-check server on $PORT alongside
the Telegram polling loop. Koyeb requires a web process that responds
on PORT — without this the service gets killed immediately.
"""

import asyncio, os, random, uuid, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from feed_reader import get_feed_posts
from ai import generate_comment, generate_news_post, generate_news_post_rephrase_with_instruction, generate_comment_rephrase_with_instruction
from poster import post_comment, scrape_comments, create_post
from db import (
    already_commented, mark_commented, save_warm_lead, daily_limit_reached, increment_today_count,
    log_target_created, log_target_action, log_target_comment_version, log_target_final,
    log_news_created, log_news_draft_added, log_news_action,
)
from dotenv import load_dotenv

load_dotenv()


# ── Health-check server (required by Koyeb) ───────────────────────────────────
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # suppress noisy access logs

def _start_health_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    print(f"[health] Listening on port {port}")
    server.serve_forever()


# All messages are sent as plain text (no parse_mode) to avoid Telegram "Can't parse entities" errors.

# ── State ─────────────────────────────────────────────────────────────────────
pending_targets    = {}     # target_id → target data
ready_comments     = {}     # comment_id → target data + draft
ready_news         = {}     # news_id → post content string
approved_queue     = asyncio.Queue()
is_scanning        = False
scan_task          = None
chat_id            = None
total_approved     = 0
total_posted       = 0
waiting_for_count  = False  # True when bot is waiting for target count
waiting_for_rephrase_news_id = None
waiting_for_rephrase_message_id = None
waiting_for_custom_comment_id = None
waiting_for_custom_comment_message_id = None
waiting_for_rephrase_comment_id = None
waiting_for_rephrase_comment_message_id = None


# ── /start_cron ───────────────────────────────────────────────────────────────
async def start_cron(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_scanning, chat_id, waiting_for_count

    if is_scanning:
        await update.message.reply_text("⚠️ A scan is already running. Send /stop first.")
        return

    chat_id = update.effective_chat.id

    if context.args and context.args[0].isdigit():
        count = min(int(context.args[0]), 20)
        await _begin_scan(update, context, count)
    else:
        waiting_for_count = True
        await update.message.reply_text("🔢 How many targets do you want? (1-20)")


def _comment_preview_text(comment_data: dict) -> str:
    author_name = comment_data.get("author_name", "Unknown")
    author_title = comment_data.get("author_title", "")
    url = comment_data.get("url", "")
    post_text = (comment_data.get("text") or "")[:200]
    draft = comment_data.get("draft", "")
    return (
        f"👤 {author_name} — {author_title}\n\n"
        f"🔗 {url}\n\n"
        f"📝 {post_text}...\n\n"
        f"💬 Your comment:\n{draft}"
    )


def _comment_keyboard(comment_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Post", callback_data=f"confirm_{comment_id}"),
            InlineKeyboardButton("❌ Drop", callback_data=f"drop_{comment_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Regenerate", callback_data=f"regen_{comment_id}"),
            InlineKeyboardButton("📝 Custom", callback_data=f"customcomment_{comment_id}"),
            InlineKeyboardButton("✏️ Rephrase", callback_data=f"repcomment_{comment_id}"),
        ],
    ])


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_count
    global waiting_for_rephrase_news_id, waiting_for_rephrase_message_id
    global waiting_for_custom_comment_id, waiting_for_custom_comment_message_id
    global waiting_for_rephrase_comment_id, waiting_for_rephrase_comment_message_id

    text = (update.message.text or "").strip()

    # Custom comment (user types their own)
    if waiting_for_custom_comment_id is not None:
        if text.lower() == "/cancel":
            waiting_for_custom_comment_id = None
            waiting_for_custom_comment_message_id = None
            await update.message.reply_text("Cancelled.")
            return
        comment_id = waiting_for_custom_comment_id
        comment_data = ready_comments.get(comment_id)
        if not comment_data:
            waiting_for_custom_comment_id = None
            waiting_for_custom_comment_message_id = None
            await update.message.reply_text("This comment expired.")
            return
        message_id = waiting_for_custom_comment_message_id
        waiting_for_custom_comment_id = None
        waiting_for_custom_comment_message_id = None
        custom_draft = text
        comment_data["draft"] = custom_draft
        ready_comments[comment_id] = comment_data
        log_id = comment_data.get("log_id")
        if log_id:
            try:
                log_target_comment_version(log_id, custom_draft)
            except Exception as e:
                print(f"[bot] log comment version error: {e}")
        preview = _comment_preview_text(comment_data)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id,
            text=preview,
            reply_markup=_comment_keyboard(comment_id),
        )
        await update.message.reply_text("✅ Custom comment set. See above.")
        return

    # Rephrase comment with instruction
    if waiting_for_rephrase_comment_id is not None:
        if text.lower() == "/cancel":
            waiting_for_rephrase_comment_id = None
            waiting_for_rephrase_comment_message_id = None
            await update.message.reply_text("Cancelled.")
            return
        comment_id = waiting_for_rephrase_comment_id
        comment_data = ready_comments.get(comment_id)
        if not comment_data:
            waiting_for_rephrase_comment_id = None
            waiting_for_rephrase_comment_message_id = None
            await update.message.reply_text("This comment expired.")
            return
        instruction = text
        message_id = waiting_for_rephrase_comment_message_id
        waiting_for_rephrase_comment_id = None
        waiting_for_rephrase_comment_message_id = None
        await update.message.reply_text("⏳ Rephrasing your comment...")
        loop = asyncio.get_event_loop()
        current_draft = comment_data.get("draft", "")
        new_draft = await loop.run_in_executor(
            None,
            lambda: generate_comment_rephrase_with_instruction(current_draft, instruction),
        )
        if not new_draft:
            await update.message.reply_text("❌ Rephrase failed. Try again or Regenerate.")
            return
        comment_data["draft"] = new_draft
        ready_comments[comment_id] = comment_data
        log_id = comment_data.get("log_id")
        if log_id:
            try:
                log_target_comment_version(log_id, new_draft)
            except Exception as e:
                print(f"[bot] log comment version error: {e}")
        preview = _comment_preview_text(comment_data)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id,
            text=preview,
            reply_markup=_comment_keyboard(comment_id),
        )
        await update.message.reply_text("✅ Rephrased. See the updated comment above.")
        return

    # Rephrase news instruction
    if waiting_for_rephrase_news_id is not None:
        if text.lower() == "/cancel":
            waiting_for_rephrase_news_id = None
            waiting_for_rephrase_message_id = None
            await update.message.reply_text("Cancelled.")
            return
        news_id = waiting_for_rephrase_news_id
        news_data = ready_news.get(news_id)
        if not news_data:
            waiting_for_rephrase_news_id = None
            waiting_for_rephrase_message_id = None
            await update.message.reply_text("Session expired. Run /post_news again.")
            return
        instruction = text
        message_id = waiting_for_rephrase_message_id
        waiting_for_rephrase_news_id = None
        waiting_for_rephrase_message_id = None
        await update.message.reply_text("⏳ Rephrasing news post...")
        loop = asyncio.get_event_loop()
        search_ctx = news_data.get("search_context", "")
        current_content = news_data.get("content", "")
        new_content = await loop.run_in_executor(
            None,
            lambda: generate_news_post_rephrase_with_instruction(search_ctx, current_content, instruction),
        )
        if not new_content:
            await update.message.reply_text("❌ Rephrase failed. Try /post_news again.")
            return
        news_data["content"] = new_content
        ready_news[news_id] = news_data
        log_id = news_data.get("log_id")
        if log_id:
            try:
                log_news_draft_added(log_id, new_content, "rephrase_instruction", None)
            except Exception as e:
                print(f"[bot] log news draft error: {e}")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Post to LinkedIn", callback_data=f"postnews_{news_id}"),
                InlineKeyboardButton("❌ Drop", callback_data=f"dropnews_{news_id}"),
            ],
            [
                InlineKeyboardButton("✏️ Rephrase", callback_data=f"repnews_{news_id}"),
                InlineKeyboardButton("🔃 Fetch again", callback_data=f"fetchnews_{news_id}"),
            ],
        ])
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id,
            text=f"📰 Draft LinkedIn Post (rephrased):\n\n{new_content}",
            reply_markup=keyboard,
        )
        await update.message.reply_text("✅ Rephrased. See the updated draft above.")
        return

    # Target count input
    if waiting_for_count:
        if text.isdigit():
            count = min(int(text), 20)
            waiting_for_count = False
            await _begin_scan(update, context, count)
        else:
            await update.message.reply_text("Please enter a number between 1 and 20.")


# ── Begin scan ────────────────────────────────────────────────────────────────
async def _begin_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, count: int):
    global is_scanning, scan_task

    is_scanning = True
    await update.message.reply_text(f"🔍 Scanning LinkedIn for {count} targets... this takes a few minutes.")

    loop = asyncio.get_event_loop()
    scan_task = asyncio.create_task(_run_scan(context.bot, count, loop))


async def _run_scan(bot, max_posts: int, loop):
    global is_scanning

    try:
        posts = []

        async def _collect(post_data):
            posts.append(post_data)

        await get_feed_posts(_collect, max_targets=max_posts)

        if not posts:
            if chat_id:
                await bot.send_message(
                    chat_id=chat_id,
                    text="😕 No matching posts found this scan. Try /start_cron again later.",
                )
            return

        await bot.send_message(
            chat_id=chat_id,
            text=f"✅ Found {len(posts)} matching post(s). Sending for review...",
        )

        for post in posts:
            if not is_scanning:
                break
            target_id = str(uuid.uuid4())[:8]
            author_name = post.get("author_name", "Unknown")
            author_title = post.get("author_title", "")
            post_text = post.get("post_text", "")
            url = post.get("url", "")

            pending_targets[target_id] = {
                "author_name":  author_name,
                "author_title": author_title,
                "text":         post_text,
                "url":          url,
                "target_id":    target_id,
            }

            msg_text = (
                f"🎯 Target\n\n"
                f"👤 {author_name} — {author_title}\n\n"
                f"🔗 {url}\n\n"
                f"📝 {post_text[:200]}..."
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_{target_id}"),
                    InlineKeyboardButton("❌ Skip", callback_data=f"skip_{target_id}"),
                ],
            ])

            tele_msg = await bot.send_message(
                chat_id=chat_id,
                text=msg_text,
                reply_markup=keyboard,
            )

            log_id = None
            try:
                log_id = log_target_created(
                    raw_text=post_text,
                    url=url,
                    author_name=author_name,
                    author_title=author_title,
                    reason=post.get("reason", ""),
                    tele_msg=msg_text,
                    target_id=target_id,
                )
            except Exception as e:
                print(f"[bot] log target created error: {e}")

            pending_targets[target_id]["log_id"] = log_id

    except Exception as e:
        print(f"[bot] Scan error: {e}")
        if chat_id:
            await bot.send_message(
                chat_id=chat_id,
                text=f"❌ Scan failed: {e}",
            )
    finally:
        is_scanning = False


# ── Button handler ────────────────────────────────────────────────────────────
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_rephrase_news_id, waiting_for_rephrase_message_id
    global waiting_for_custom_comment_id, waiting_for_custom_comment_message_id
    global waiting_for_rephrase_comment_id, waiting_for_rephrase_comment_message_id
    global total_approved

    query = update.callback_query
    await query.answer()
    data = query.data

    # ── Target buttons ────────────────────────────────────────────────────────
    if data.startswith("approve_"):
        target_id = data[len("approve_"):]
        target_data = pending_targets.get(target_id)
        if not target_data:
            await query.edit_message_text("This target expired.")
            return
        total_approved += 1
        log_id = target_data.get("log_id")
        if log_id:
            try:
                log_target_action(log_id, "approved")
            except Exception as e:
                print(f"[bot] log approve error: {e}")
        await query.edit_message_text(
            text=f"⏳ Generating comment for {target_data['author_name']}..."
        )
        asyncio.create_task(_generate_and_show_comment(context.bot, target_data, query.message.message_id))

    elif data.startswith("skip_"):
        target_id = data[len("skip_"):]
        target_data = pending_targets.pop(target_id, None)
        log_id = target_data.get("log_id") if target_data else None
        if log_id:
            try:
                log_target_action(log_id, "skipped")
                log_target_final(log_id, "skipped")
            except Exception as e:
                print(f"[bot] log skip error: {e}")
        await query.edit_message_text("⏭️ Skipped.")

    # ── Comment buttons ───────────────────────────────────────────────────────
    elif data.startswith("confirm_"):
        comment_id = data[len("confirm_"):]
        comment_data = ready_comments.get(comment_id)
        if not comment_data:
            await query.edit_message_text("This comment expired.")
            return
        log_id = comment_data.get("log_id")
        if log_id:
            try:
                log_target_action(log_id, "queued")
            except Exception as e:
                print(f"[bot] log queued error: {e}")
        await approved_queue.put(comment_data)
        await query.edit_message_text(
            text=f"✅ Queued! Comment on {comment_data['author_name']}'s post.\nQueue size: {approved_queue.qsize()}"
        )

    elif data.startswith("drop_"):
        comment_id = data[len("drop_"):]
        comment_data = ready_comments.pop(comment_id, None)
        log_id = comment_data.get("log_id") if comment_data else None
        if log_id:
            try:
                log_target_action(log_id, "dropped")
                log_target_final(log_id, "dropped")
            except Exception as e:
                print(f"[bot] log drop error: {e}")
        await query.edit_message_text("🗑️ Dropped.")

    elif data.startswith("regen_"):
        comment_id = data[len("regen_"):]
        comment_data = ready_comments.get(comment_id)
        if not comment_data:
            await query.edit_message_text("This comment expired.")
            return
        await query.edit_message_text(
            text=f"🔄 Regenerating comment for {comment_data['author_name']}..."
        )
        asyncio.create_task(_generate_and_show_comment(context.bot, comment_data, query.message.message_id, comment_id=comment_id))

    elif data.startswith("customcomment_"):
        comment_id = data[len("customcomment_"):]
        comment_data = ready_comments.get(comment_id)
        if not comment_data:
            await query.edit_message_text("This comment expired.")
            return
        waiting_for_custom_comment_id = comment_id
        waiting_for_custom_comment_message_id = query.message.message_id
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="📝 Type your custom comment (or /cancel):",
        )

    elif data.startswith("repcomment_"):
        comment_id = data[len("repcomment_"):]
        comment_data = ready_comments.get(comment_id)
        if not comment_data:
            await query.edit_message_text("This comment expired.")
            return
        waiting_for_rephrase_comment_id = comment_id
        waiting_for_rephrase_comment_message_id = query.message.message_id
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✏️ Give a rephrase instruction (e.g. 'make it shorter', 'more formal'):",
        )

    # ── News buttons ──────────────────────────────────────────────────────────
    elif data.startswith("postnews_"):
        news_id = data[len("postnews_"):]
        news_data = ready_news.get(news_id)
        if not news_data:
            await query.edit_message_text("This news post expired.")
            return
        content = news_data.get("content", "")
        log_id = news_data.get("log_id")
        await query.edit_message_text("⏳ Posting to LinkedIn...")
        success = await create_post(content)
        if success:
            if log_id:
                try:
                    log_news_action(log_id, "posted", content)
                except Exception as e:
                    print(f"[bot] log news post error: {e}")
            await query.edit_message_text("✅ Posted to LinkedIn!")
        else:
            await query.edit_message_text("❌ Failed to post. Check logs.")

    elif data.startswith("dropnews_"):
        news_id = data[len("dropnews_"):]
        news_data = ready_news.pop(news_id, None)
        log_id = news_data.get("log_id") if news_data else None
        if log_id:
            try:
                log_news_action(log_id, "dropped")
            except Exception as e:
                print(f"[bot] log news drop error: {e}")
        await query.edit_message_text("🗑️ News post dropped.")

    elif data.startswith("repnews_"):
        news_id = data[len("repnews_"):]
        news_data = ready_news.get(news_id)
        if not news_data:
            await query.edit_message_text("This news post expired.")
            return
        waiting_for_rephrase_news_id = news_id
        waiting_for_rephrase_message_id = query.message.message_id
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✏️ Give a rephrase instruction (e.g. 'make it more casual', 'focus on funding only'):",
        )

    elif data.startswith("fetchnews_"):
        news_id = data[len("fetchnews_"):]
        await query.edit_message_text("🔃 Fetching fresh news...")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, generate_news_post)
        content = result.get("content", "")
        search_ctx = result.get("search_context", "")
        if not content:
            await query.edit_message_text("❌ Failed to fetch news. Try /post_news again.")
            return
        new_news_id = str(uuid.uuid4())[:8]
        log_id = None
        try:
            log_id = log_news_created(search_ctx, content, "fetch", content, new_news_id)
        except Exception as e:
            print(f"[bot] log news created error: {e}")
        ready_news[new_news_id] = {"content": content, "search_context": search_ctx, "log_id": log_id}
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Post to LinkedIn", callback_data=f"postnews_{new_news_id}"),
                InlineKeyboardButton("❌ Drop", callback_data=f"dropnews_{new_news_id}"),
            ],
            [
                InlineKeyboardButton("✏️ Rephrase", callback_data=f"repnews_{new_news_id}"),
                InlineKeyboardButton("🔃 Fetch again", callback_data=f"fetchnews_{new_news_id}"),
            ],
        ])
        await query.edit_message_text(
            text=f"📰 Fresh Draft:\n\n{content}",
            reply_markup=keyboard,
        )


# ── /post_news ────────────────────────────────────────────────────────────────
async def post_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔍 Searching for top AI news this week...")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, generate_news_post)
    content = result.get("content", "")
    search_ctx = result.get("search_context", "")

    if not content:
        await update.message.reply_text("❌ Failed to generate news post. Try again.")
        return

    news_id = str(uuid.uuid4())[:8]
    log_id = None
    try:
        log_id = log_news_created(search_ctx, content, "fetch", content, news_id)
    except Exception as e:
        print(f"[bot] log news created error: {e}")

    ready_news[news_id] = {"content": content, "search_context": search_ctx, "log_id": log_id}

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Post to LinkedIn", callback_data=f"postnews_{news_id}"),
            InlineKeyboardButton("❌ Drop", callback_data=f"dropnews_{news_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Rephrase", callback_data=f"repnews_{news_id}"),
            InlineKeyboardButton("🔃 Fetch again", callback_data=f"fetchnews_{news_id}"),
        ],
    ])

    await update.message.reply_text(
        text=f"📰 Draft LinkedIn Post:\n\n{content}",
        reply_markup=keyboard,
    )


# ── Generate + show comment ───────────────────────────────────────────────────
async def _generate_and_show_comment(bot, target_data: dict, message_id: int, comment_id: str = None):
    author_name  = target_data.get("author_name", "Unknown")
    author_title = target_data.get("author_title", "")
    post_text    = target_data.get("text", "")
    url          = target_data.get("url", "")

    existing_comments = []
    try:
        loop = asyncio.get_event_loop()
        existing_comments = await loop.run_in_executor(None, lambda: scrape_comments(url)) if url else []
    except Exception as e:
        print(f"[bot] Comment scraping error: {e}")
        existing_comments = []

    loop = asyncio.get_event_loop()
    comment = await loop.run_in_executor(
        None, lambda: generate_comment(post_text, author_title, existing_comments)
    )

    if not comment:
        if chat_id and message_id:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"❌ Failed to generate comment for {author_name}'s post.",
            )
        return

    log_id = target_data.get("log_id")
    if log_id:
        try:
            log_target_comment_version(log_id, comment)
        except Exception as e:
            print(f"[bot] log comment version error: {e}")

    if comment_id is None:
        comment_id = str(uuid.uuid4())[:8]

    ready_comments[comment_id] = {
        **target_data,
        "draft":            comment,
        "existing_comments": existing_comments,
        "log_id":           log_id,
    }

    preview_text = _comment_preview_text(ready_comments[comment_id])
    keyboard = _comment_keyboard(comment_id)

    if chat_id and message_id:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=preview_text,
            reply_markup=keyboard,
        )
    elif chat_id:
        await bot.send_message(
            chat_id=chat_id,
            text=preview_text,
            reply_markup=keyboard,
        )


# ── Background Worker ─────────────────────────────────────────────────────────
async def worker(app):
    global total_posted

    while True:
        target_data = await approved_queue.get()

        url     = target_data["url"]
        comment = target_data["draft"]
        author  = target_data.get("author_name", "Unknown")

        if daily_limit_reached():
            if chat_id:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text="🛑 Daily limit (10) reached. Queue paused until tomorrow.",
                )
            await approved_queue.put(target_data)
            await asyncio.sleep(3600)
            continue

        if chat_id:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"⏳ Posting comment on {author}'s post...",
            )

        success = await post_comment(url, comment)

        if success:
            total_posted += 1
            mark_commented(url)
            increment_today_count()
            log_id = target_data.get("log_id")
            if log_id:
                try:
                    log_target_action(log_id, "posted")
                    log_target_final(log_id, "posted", comment)
                except Exception as e:
                    print(f"[bot] log posted error: {e}")
            save_warm_lead(
                author_name=target_data.get("author_name", ""),
                author_title=target_data.get("author_title", ""),
                post_snippet=target_data.get("text", "")[:300],
                comment=comment,
            )
            remaining = approved_queue.qsize()
            if chat_id:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ Comment {total_posted} posted on {author}'s post!\n"
                        f"📊 Queue: {remaining} remaining."
                    ),
                )
        else:
            if chat_id:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Failed to post on {author}'s post.",
                )

        approved_queue.task_done()

        if not approved_queue.empty():
            wait = random.randint(300, 600)
            if chat_id:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏰ Safety delay: waiting {wait // 60}m {wait % 60}s before next...",
                )
            await asyncio.sleep(wait)
        else:
            if chat_id and total_posted > 0:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"🏁 All done! Posted {total_posted} comments.\nSend /start_cron for the next round.",
                )


# ── /cancel ───────────────────────────────────────────────────────────────────
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_rephrase_news_id, waiting_for_rephrase_message_id
    global waiting_for_custom_comment_id, waiting_for_custom_comment_message_id
    global waiting_for_rephrase_comment_id, waiting_for_rephrase_comment_message_id
    waiting_for_rephrase_news_id = None
    waiting_for_rephrase_message_id = None
    waiting_for_custom_comment_id = None
    waiting_for_custom_comment_message_id = None
    waiting_for_rephrase_comment_id = None
    waiting_for_rephrase_comment_message_id = None
    await update.message.reply_text("Cancelled.")


# ── /stop ─────────────────────────────────────────────────────────────────────
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_scanning, scan_task, total_approved, total_posted, waiting_for_count
    global waiting_for_rephrase_news_id, waiting_for_rephrase_message_id
    global waiting_for_custom_comment_id, waiting_for_custom_comment_message_id
    global waiting_for_rephrase_comment_id, waiting_for_rephrase_comment_message_id

    if scan_task and not scan_task.done():
        scan_task.cancel()

    is_scanning = False
    waiting_for_count = False
    waiting_for_rephrase_news_id = None
    waiting_for_rephrase_message_id = None
    waiting_for_custom_comment_id = None
    waiting_for_custom_comment_message_id = None
    waiting_for_rephrase_comment_id = None
    waiting_for_rephrase_comment_message_id = None

    cleared = 0
    while not approved_queue.empty():
        try:
            approved_queue.get_nowait()
            cleared += 1
        except asyncio.QueueEmpty:
            break

    pending_targets.clear()
    ready_comments.clear()
    ready_news.clear()
    total_approved = 0
    total_posted = 0

    await update.message.reply_text(
        f"🛑 Stopped. Cleared {cleared} queued items.\nSend /start_cron to start again.",
    )


# ── Startup ───────────────────────────────────────────────────────────────────
async def post_init(app):
    asyncio.create_task(worker(app))


if __name__ == "__main__":
    # Start health-check server in a background thread (required by Koyeb)
    health_thread = threading.Thread(target=_start_health_server, daemon=True)
    health_thread.start()

    app = (
        ApplicationBuilder()
        .token(os.environ["TELEGRAM_TOKEN"])
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start_cron", start_cron))
    app.add_handler(CommandHandler("post_news", post_news))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🤖 Bot is running. Commands: /start_cron, /post_news, /stop")
    app.run_polling()