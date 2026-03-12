"""
bot.py — LinkedIn Networking Bot (Telegram-controlled)

Commands:
  /start_cron       — Ask how many targets, then scan feed
  /post_news        — Generate an AI news post for LinkedIn
  /stop             — Cancel everything

Flow:
  1. /start_cron → asks how many targets → scans feed → sends each live
  2. User approves → bot scrapes comments → generates contextual comment → shows for confirmation
  3. User confirms (Post / Regenerate / Drop) → queued for background posting
  4. Worker posts with 5–10 min delays + live updates
"""

import asyncio, os, random, uuid
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
waiting_for_custom_comment_id = None      # comment_id when waiting for user to type their own comment
waiting_for_custom_comment_message_id = None
waiting_for_rephrase_comment_id = None    # comment_id when waiting for rephrase instruction
waiting_for_rephrase_comment_message_id = None


# ── /start_cron ───────────────────────────────────────────────────────────────
async def start_cron(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_scanning, chat_id, waiting_for_count

    if is_scanning:
        await update.message.reply_text("⚠️ A scan is already running. Send /stop first.")
        return

    chat_id = update.effective_chat.id

    # Check if count was passed as argument: /start_cron 5
    if context.args and context.args[0].isdigit():
        count = min(int(context.args[0]), 20)
        await _begin_scan(update, context, count)
    else:
        waiting_for_count = True
        await update.message.reply_text("🔢 How many targets do you want? (1-20)")


def _comment_preview_text(comment_data: dict) -> str:
    """Build the comment card text for display."""
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
    """Keyboard for the comment draft card."""
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
    """Handle plain text: target count, custom comment, rephrase comment, or rephrase news."""
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
            await update.message.reply_text("This draft expired. Use /post_news again.")
            return
        instruction = text
        waiting_for_rephrase_news_id = None
        message_id = waiting_for_rephrase_message_id
        waiting_for_rephrase_message_id = None
        await update.message.reply_text("⏳ Rephrasing with your instruction...")
        loop = asyncio.get_event_loop()
        search_ctx = news_data.get("search_context", "") if isinstance(news_data, dict) else ""
        current = news_data.get("content", "") if isinstance(news_data, dict) else news_data
        new_content = await loop.run_in_executor(
            None,
            lambda: generate_news_post_rephrase_with_instruction(search_ctx, current, instruction),
        )
        if not new_content:
            await update.message.reply_text("❌ Rephrase failed. Try again or use Fetch again.")
            return
        news_log_id = news_data.get("log_id") if isinstance(news_data, dict) else None
        if news_log_id:
            try:
                log_news_draft_added(news_log_id, new_content, "rephrase", f"📰 Draft LinkedIn Post:\n\n{new_content}")
            except Exception as e:
                print(f"[bot] log_news_draft_added rephrase error: {e}")
        ready_news[news_id] = {"content": new_content, "search_context": search_ctx, "log_id": news_log_id}
        keyboard = _news_keyboard(news_id)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id,
            text=f"📰 Draft LinkedIn Post:\n\n{new_content}",
            reply_markup=keyboard,
        )
        await update.message.reply_text("✅ Rephrased. See the updated draft above.")
        return

    # Target count
    if not waiting_for_count:
        return
    if text.isdigit() and 1 <= int(text) <= 20:
        waiting_for_count = False
        await _begin_scan(update, context, int(text))
    else:
        await update.message.reply_text("Please send a number between 1 and 20.")


async def _begin_scan(update, context, target_count):
    global is_scanning, scan_task, total_approved, total_posted, chat_id

    if daily_limit_reached():
        await update.message.reply_text("🛑 Daily comment limit (10) reached. Try again tomorrow.")
        return

    chat_id = update.effective_chat.id
    is_scanning = True
    total_approved = 0
    total_posted = 0
    pending_targets.clear()

    await update.message.reply_text(
        f"🚀 Scanning for {target_count} targets...\n"
        f"I'll send them as I find them."
    )

    scan_task = asyncio.create_task(_run_scan(context.bot, target_count))


async def _run_scan(bot, target_count):
    """Background scan — sends targets without drafts."""
    global is_scanning
    found_count = 0

    async def on_target_found(data):
        nonlocal found_count

        if already_commented(data["url"]):
            return

        found_count += 1
        target_id = str(uuid.uuid4())[:8]
        author = data.get("author_name", "Unknown")
        title  = data.get("author_title", "")
        reason = data.get("reason", "N/A")
        msg = (
            f"🎯 Target #{found_count}\n\n"
            f"👤 {author} — {title}\n\n"
            f"🔗 {data['url']}\n\n"
            f"📝 {data['text'][:250]}...\n\n"
            f"💡 Why: {reason}"
        )
        raw_text = data.get("raw_text", data["text"])
        try:
            log_id = log_target_created(
                raw_text=raw_text,
                url=data["url"],
                author_name=author,
                author_title=title,
                reason=reason,
                tele_msg=msg,
                target_id=target_id,
            )
        except Exception as e:
            log_id = None
            print(f"[bot] log_target_created error: {e}")
        pending_targets[target_id] = {
            "url":          data["url"],
            "text":         data["text"],
            "author_name":  author,
            "author_title": title,
            "reason":       reason,
            "log_id":       log_id,
        }

        keyboard = [[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{target_id}"),
            InlineKeyboardButton("❌ Skip",    callback_data=f"skip_{target_id}"),
        ]]

        await bot.send_message(
            chat_id=chat_id,
            text=msg,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    try:
        await get_feed_posts(on_target_found, max_targets=target_count)
    except asyncio.CancelledError:
        await bot.send_message(chat_id=chat_id, text="🛑 Scan cancelled.")
        return
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❌ Scan error: {e}")
    finally:
        is_scanning = False

    await bot.send_message(
        chat_id=chat_id,
        text=f"🏁 Scan complete! Found {found_count} targets.\nApprove the ones you want to comment on.",
    )


# ── /post_news ────────────────────────────────────────────────────────────────
async def post_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id

    await update.message.reply_text("🔍 Searching latest AI news and drafting a post...")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, generate_news_post)
    content = result.get("content", "") if isinstance(result, dict) else (result or "")
    search_context = result.get("search_context", "") if isinstance(result, dict) else ""

    if not content:
        await update.message.reply_text("❌ Failed to generate news post. Try again.")
        return

    news_id = str(uuid.uuid4())[:8]
    tele_msg = f"📰 Draft LinkedIn Post:\n\n{content}"
    try:
        news_log_id = log_news_created(
            search_raw=search_context,
            draft_content=content,
            source="fetch",
            tele_msg=tele_msg,
            news_id=news_id,
        )
    except Exception as e:
        news_log_id = None
        print(f"[bot] log_news_created error: {e}")
    ready_news[news_id] = {"content": content, "search_context": search_context, "log_id": news_log_id}

    keyboard = [
        [
            InlineKeyboardButton("✅ Post to LinkedIn", callback_data=f"postnews_{news_id}"),
            InlineKeyboardButton("❌ Drop", callback_data=f"dropnews_{news_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Regenerate", callback_data=f"regennews_{news_id}"),
            InlineKeyboardButton("🔃 Fetch again", callback_data=f"fetchnews_{news_id}"),
        ],
    ]

    await update.message.reply_text(tele_msg, reply_markup=InlineKeyboardMarkup(keyboard))


# ── Button handler ────────────────────────────────────────────────────────────
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global total_approved, waiting_for_custom_comment_id, waiting_for_custom_comment_message_id
    global waiting_for_rephrase_comment_id, waiting_for_rephrase_comment_message_id
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── Target approve/skip ──
    if data.startswith("approve_"):
        target_id = data.replace("approve_", "")
        target_data = pending_targets.pop(target_id, None)

        if not target_data:
            await query.edit_message_text("⚠️ This target has expired.")
            return

        total_approved += 1
        author = target_data.get("author_name", "Unknown")
        log_id = target_data.get("log_id")
        if log_id:
            try:
                log_target_action(log_id, "approve")
            except Exception as e:
                print(f"[bot] log approve error: {e}")
        await query.edit_message_text(
            f"✅ Approved #{total_approved} — {author}\n"
            f"⏳ Generating comment (reading other comments + web search)...",
        )

        msg_id = query.message.message_id
        asyncio.create_task(_prepare_comment(context.bot, target_data, msg_id))

    elif data.startswith("skip_"):
        target_id = data.replace("skip_", "")
        target_data = pending_targets.pop(target_id, None)
        if target_data:
            log_id = target_data.get("log_id")
            if log_id:
                try:
                    log_target_action(log_id, "skip")
                    log_target_final(log_id, "skipped")
                except Exception as e:
                    print(f"[bot] log skip error: {e}")
            author = target_data.get("author_name", "Unknown")
            title = target_data.get("author_title", "")
            skip_msg = (
                f"🎯 Target\n\n"
                f"👤 {author} — {title}\n\n"
                f"🔗 {target_data['url']}\n\n"
                f"📝 {target_data['text'][:250]}...\n\n"
                f"💡 Why: {target_data.get('reason', 'N/A')}\n\n"
                f"❌ Skipped."
            )
            await query.edit_message_text(skip_msg)
        else:
            await query.edit_message_text("❌ Skipped (target already gone).")

    # ── Comment confirm/regen/drop ──
    elif data.startswith("confirm_"):
        comment_id = data.replace("confirm_", "")
        comment_data = ready_comments.pop(comment_id, None)
        if not comment_data:
            await query.edit_message_text("⚠️ This comment has expired.")
            return
        log_id = comment_data.get("log_id")
        if log_id:
            try:
                log_target_action(log_id, "queued")
                log_target_final(log_id, "queued", comment_data["draft"])
            except Exception as e:
                print(f"[bot] log queued error: {e}")
        await approved_queue.put(comment_data)
        author_name = comment_data.get("author_name", "Unknown")
        queued_msg = (
            f"👤 {author_name} — {comment_data.get('author_title', '')}\n\n"
            f"💬 Your comment:\n{comment_data['draft'][:300]}\n\n"
            f"📤 Queued for posting."
        )
        await query.edit_message_text(queued_msg)

    elif data.startswith("regen_"):
        comment_id = data.replace("regen_", "")
        comment_data = ready_comments.pop(comment_id, None)
        if not comment_data:
            await query.edit_message_text("⚠️ This comment has expired.")
            return
        msg_id = query.message.message_id
        await query.edit_message_text("🔄 Regenerating comment...")
        asyncio.create_task(_prepare_comment(context.bot, comment_data, msg_id))

    elif data.startswith("customcomment_"):
        comment_id = data.replace("customcomment_", "")
        comment_data = ready_comments.get(comment_id)
        if not comment_data:
            await query.edit_message_text("⚠️ This comment has expired.")
            return
        waiting_for_custom_comment_id = comment_id
        waiting_for_custom_comment_message_id = query.message.message_id
        await query.edit_message_text(
            "📝 Send your own comment in the next message. It will replace the draft.\n\n"
            "Send /cancel to cancel."
        )

    elif data.startswith("repcomment_"):
        comment_id = data.replace("repcomment_", "")
        comment_data = ready_comments.get(comment_id)
        if not comment_data:
            await query.edit_message_text("⚠️ This comment has expired.")
            return
        waiting_for_rephrase_comment_id = comment_id
        waiting_for_rephrase_comment_message_id = query.message.message_id
        await query.edit_message_text(
            "✏️ How would you like to rephrase? Send your instruction in the next message.\n\n"
            "Examples: \"make it shorter\", \"more formal\", \"add a question\", \"friendlier tone\".\n\n"
            "Send /cancel to cancel."
        )

    elif data.startswith("drop_"):
        comment_id = data.replace("drop_", "")
        comment_data = ready_comments.pop(comment_id, None)
        if comment_data:
            log_id = comment_data.get("log_id")
            if log_id:
                try:
                    log_target_action(log_id, "drop")
                    log_target_final(log_id, "dropped")
                except Exception as e:
                    print(f"[bot] log drop comment error: {e}")
            author_name = comment_data.get("author_name", "Unknown")
            author_title = comment_data.get("author_title", "")
            url = comment_data.get("url", "")
            post_text = comment_data.get("text", "")[:200]
            draft = comment_data.get("draft", "")
            drop_msg = (
                f"👤 {author_name} — {author_title}\n\n"
                f"🔗 {url}\n\n"
                f"📝 {post_text}...\n\n"
                f"💬 Your comment:\n{draft}\n\n"
                f"🗑 Dropped."
            )
            await query.edit_message_text(drop_msg)
        else:
            await query.edit_message_text("🗑 Dropped.")

    # ── News post/regen/fetch/drop ──
    elif data.startswith("postnews_"):
        news_id = data.replace("postnews_", "")
        news_data = ready_news.pop(news_id, None)
        if not news_data:
            await query.edit_message_text("⚠️ This post has expired.")
            return
        content = news_data["content"] if isinstance(news_data, dict) else news_data
        news_log_id = news_data.get("log_id") if isinstance(news_data, dict) else None
        await query.edit_message_text("⏳ Publishing to LinkedIn...")
        asyncio.create_task(_publish_news(context.bot, content, query.message.message_id, news_log_id))

    elif data.startswith("repnews_"):
        news_id = data.replace("repnews_", "")
        news_data = ready_news.get(news_id)
        if not news_data:
            await query.edit_message_text("⚠️ This post has expired.")
            return
        global waiting_for_rephrase_news_id, waiting_for_rephrase_message_id
        waiting_for_rephrase_news_id = news_id
        waiting_for_rephrase_message_id = query.message.message_id
        await query.edit_message_text(
            "✏️ How would you like to rephrase? Send your instruction in the next message.\n\n"
            "Examples: \"make it more casual\", \"focus on funding only\", \"shorter and punchier\", \"add a question at the end\".\n\n"
            "Send /cancel to cancel."
        )

    elif data.startswith("fetchnews_"):
        news_id = data.replace("fetchnews_", "")
        ready_news.pop(news_id, None)
        await query.edit_message_text("🔃 Fetching fresh news (Tavily + Llama)...")
        asyncio.create_task(_fetch_news(context.bot, query.message.message_id))

    elif data.startswith("dropnews_"):
        news_id = data.replace("dropnews_", "")
        news_data = ready_news.pop(news_id, None)
        if news_data:
            news_log_id = news_data.get("log_id") if isinstance(news_data, dict) else None
            if news_log_id:
                try:
                    log_news_action(news_log_id, "drop")
                except Exception as e:
                    print(f"[bot] log news drop error: {e}")
            content = news_data["content"] if isinstance(news_data, dict) else news_data
            await query.edit_message_text(
                f"📰 Draft LinkedIn Post:\n\n{content}\n\n🗑 Dropped."
            )
        else:
            await query.edit_message_text("🗑 Dropped.")


async def _publish_news(bot, content, message_id, log_id=None):
    """Publish a news post to LinkedIn."""
    success = await create_post(content)
    if log_id:
        try:
            log_news_action(log_id, "post", content_posted=content if success else None)
        except Exception as e:
            print(f"[bot] log news post error: {e}")
    if success and chat_id:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"✅ Posted to LinkedIn!\n\n{content}",
        )
    elif chat_id:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="❌ Failed to publish. Check debug_screenshots/.",
        )


def _news_keyboard(news_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Post to LinkedIn", callback_data=f"postnews_{news_id}"),
            InlineKeyboardButton("❌ Drop", callback_data=f"dropnews_{news_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Rephrase", callback_data=f"repnews_{news_id}"),
            InlineKeyboardButton("🔃 Fetch again", callback_data=f"fetchnews_{news_id}"),
        ],
    ])


async def _fetch_news(bot, message_id):
    """Fetch fresh AI news via Tavily and draft a new post with Llama."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, generate_news_post)
    content = result.get("content", "") if isinstance(result, dict) else (result or "")
    search_context = result.get("search_context", "") if isinstance(result, dict) else ""

    if not content:
        if chat_id:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="❌ Failed to fetch. Try /post_news again.",
            )
        return

    news_id = str(uuid.uuid4())[:8]
    tele_msg = f"📰 Draft LinkedIn Post:\n\n{content}"
    try:
        fetch_log_id = log_news_created(
            search_raw=search_context,
            draft_content=content,
            source="fetch",
            tele_msg=tele_msg,
            news_id=news_id,
        )
    except Exception as e:
        fetch_log_id = None
        print(f"[bot] log_news_created fetch error: {e}")
    ready_news[news_id] = {"content": content, "search_context": search_context, "log_id": fetch_log_id}

    if chat_id:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=tele_msg,
            reply_markup=_news_keyboard(news_id),
        )


# ── Comment preparation ──────────────────────────────────────────────────────
async def _prepare_comment(bot, target_data, message_id=None):
    """Scrape existing comments from the post, then generate a contextual comment (so we don't repeat others and match tone)."""
    url          = target_data["url"]
    post_text    = target_data["text"]
    author_title = target_data.get("author_title", "Professional")
    author_name  = target_data.get("author_name", "Unknown")

    try:
        existing_comments = await scrape_comments(url)
        print(f"[bot] Scraped {len(existing_comments)} comments from {author_name}'s post")
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
    comment_id = str(uuid.uuid4())[:8]
    ready_comments[comment_id] = {
        **target_data,
        "draft":             comment,
        "existing_comments":  existing_comments,
        "log_id":            log_id,
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
    """Posts approved comments one-by-one with safe delays."""
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
                    text=f"❌ Failed to post on {author}'s post. Check debug_screenshots/.",
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
    """Cancel waiting for rephrase/custom input (news or comment)."""
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
    # Text handler for capturing target count (must be added AFTER command handlers)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🤖 Bot is running. Commands: /start_cron, /post_news, /stop")
    app.run_polling()