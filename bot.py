"""
bot.py — Works on both Koyeb and PythonAnywhere.
On Koyeb: starts a health-check HTTP server on $PORT.
On PythonAnywhere: $PORT is not set, health server is skipped.
"""

import asyncio, os, random, uuid, requests, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from ai import (
    generate_comment, generate_news_post,
    generate_news_post_rephrase_with_instruction,
    generate_comment_rephrase_with_instruction,
)
from poster import post_comment, create_post
from db import (
    already_commented, mark_commented, save_warm_lead,
    daily_limit_reached, increment_today_count,
    log_target_created, log_target_action,
    log_target_comment_version, log_target_final,
    log_news_created, log_news_draft_added, log_news_action,
    get_pending_target, save_pending_post,
)
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "")


# ── Health-check server ───────────────────────────────────────────────────────
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200); self.end_headers()
    def do_POST(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *args): pass

def _start_health_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    print(f"[health] Listening on port {port}")
    server.serve_forever()


# ── GitHub Actions triggers ───────────────────────────────────────────────────
def _gh_dispatch(event_type: str, payload: dict) -> bool:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("[bot] GITHUB_TOKEN or GITHUB_REPO not set")
        return False
    r = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/dispatches",
        json={"event_type": event_type, "client_payload": payload},
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )
    if r.status_code == 204:
        return True
    print(f"[bot] GH dispatch failed: {r.status_code} {r.text}")
    return False

def trigger_scraper(target_count: int) -> bool:
    return _gh_dispatch("run_scraper", {"target_count": target_count})

def trigger_poster(post_id: str) -> bool:
    return _gh_dispatch("post_comment", {"post_id": post_id})


# ── State ─────────────────────────────────────────────────────────────────────
pending_targets    = {}
ready_comments     = {}
ready_news         = {}
is_scanning        = False
chat_id            = None
total_approved     = 0
total_posted       = 0
waiting_for_count  = False
waiting_for_rephrase_news_id            = None
waiting_for_rephrase_message_id         = None
waiting_for_custom_comment_id           = None
waiting_for_custom_comment_message_id   = None
waiting_for_rephrase_comment_id         = None
waiting_for_rephrase_comment_message_id = None


# ── Card builder ──────────────────────────────────────────────────────────────
def _build_target_card(d: dict, status: str, comment: str = None) -> str:
    """Single source of truth for target card text. Edited in-place as status changes."""
    author   = d.get("author_name", "Unknown")
    title    = d.get("author_title", "")
    conn     = d.get("connection_level", "")
    text     = (d.get("text") or "")[:300]
    likes    = d.get("likes_count", 0)
    cmts     = d.get("comments_count", 0)
    reason   = d.get("reason", "")
    idx      = d.get("target_index", "")

    parts = [f"🎯 Target #{idx}" if idx else "🎯 Target", ""]
    parts += [f"👤 {author}", f"💼 {title}"]
    if conn:
        parts.append(f"🔗 {conn} connection")
    parts += ["", f"📝 {text}...", ""]
    if likes or cmts:
        parts.append(f"❤️ {likes} likes  💬 {cmts} comments")
    parts += ["", f"📊 Status: {status}"]
    if reason and "pending" in status.lower():
        parts.append(f"💡 Why: {reason}")
    if comment:
        parts += ["", "🗨️ Your comment:", comment]
    return "\n".join(parts)


# ── Keyboards ─────────────────────────────────────────────────────────────────
def _approve_keyboard(target_id: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{target_id}"),
        InlineKeyboardButton("❌ Skip",    callback_data=f"skip_{target_id}"),
    ]])

def _comment_keyboard(comment_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Post",       callback_data=f"confirm_{comment_id}"),
            InlineKeyboardButton("❌ Drop",        callback_data=f"drop_{comment_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Regenerate", callback_data=f"regen_{comment_id}"),
            InlineKeyboardButton("📝 Custom",     callback_data=f"customcomment_{comment_id}"),
            InlineKeyboardButton("✏️ Rephrase",   callback_data=f"repcomment_{comment_id}"),
        ],
    ])

def _news_keyboard(news_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Post to LinkedIn", callback_data=f"postnews_{news_id}"),
            InlineKeyboardButton("❌ Drop",              callback_data=f"dropnews_{news_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Rephrase",         callback_data=f"repnews_{news_id}"),
            InlineKeyboardButton("🔃 Fetch again",       callback_data=f"fetchnews_{news_id}"),
        ],
    ])


# ── /start_cron ───────────────────────────────────────────────────────────────
async def start_cron(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_scanning, chat_id, waiting_for_count
    if is_scanning:
        await update.message.reply_text("⚠️ A scan is already running. Send /stop first.")
        return
    chat_id = update.effective_chat.id
    if context.args and context.args[0].isdigit():
        await _begin_scan(update, context, min(int(context.args[0]), 20))
    else:
        waiting_for_count = True
        await update.message.reply_text("🔢 How many targets do you want? (1-20)")

async def _begin_scan(update, context, target_count: int):
    global is_scanning
    if daily_limit_reached():
        await update.message.reply_text("🛑 Daily comment limit (10) reached. Try again tomorrow.")
        return
    is_scanning = True
    if trigger_scraper(target_count):
        await update.message.reply_text(
            f"🚀 Scanning for {target_count} targets via GitHub Actions...\n"
            f"Results will appear here in ~2-3 minutes."
        )
    else:
        is_scanning = False
        await update.message.reply_text("❌ Failed to trigger scraper. Check GITHUB_TOKEN and GITHUB_REPO.")


# ── Text handler ──────────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_count
    global waiting_for_rephrase_news_id, waiting_for_rephrase_message_id
    global waiting_for_custom_comment_id, waiting_for_custom_comment_message_id
    global waiting_for_rephrase_comment_id, waiting_for_rephrase_comment_message_id

    text = (update.message.text or "").strip()

    # ── Custom comment input ──────────────────────────────────────────────────
    if waiting_for_custom_comment_id is not None:
        if text.lower() == "/cancel":
            waiting_for_custom_comment_id = None
            waiting_for_custom_comment_message_id = None
            await update.message.reply_text("Cancelled.")
            return
        comment_id   = waiting_for_custom_comment_id
        comment_data = ready_comments.get(comment_id)
        if not comment_data:
            waiting_for_custom_comment_id = None
            waiting_for_custom_comment_message_id = None
            await update.message.reply_text("This comment expired.")
            return
        message_id = waiting_for_custom_comment_message_id
        waiting_for_custom_comment_id = None
        waiting_for_custom_comment_message_id = None
        comment_data["draft"] = text
        ready_comments[comment_id] = comment_data
        log_id = comment_data.get("log_id")
        if log_id:
            try: log_target_comment_version(log_id, text)
            except Exception as e: print(f"[bot] log error: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=message_id,
            text=_build_target_card(comment_data, "💬 Comment ready", text),
            reply_markup=_comment_keyboard(comment_id),
        )
        await update.message.reply_text("✅ Custom comment set. See above.")
        return

    # ── Rephrase comment input ────────────────────────────────────────────────
    if waiting_for_rephrase_comment_id is not None:
        if text.lower() == "/cancel":
            waiting_for_rephrase_comment_id = None
            waiting_for_rephrase_comment_message_id = None
            await update.message.reply_text("Cancelled.")
            return
        comment_id   = waiting_for_rephrase_comment_id
        comment_data = ready_comments.get(comment_id)
        if not comment_data:
            waiting_for_rephrase_comment_id = None
            waiting_for_rephrase_comment_message_id = None
            await update.message.reply_text("This comment expired.")
            return
        message_id = waiting_for_rephrase_comment_message_id
        waiting_for_rephrase_comment_id = None
        waiting_for_rephrase_comment_message_id = None
        await update.message.reply_text("⏳ Rephrasing...")
        loop = asyncio.get_event_loop()
        new_draft = await loop.run_in_executor(
            None, lambda: generate_comment_rephrase_with_instruction(comment_data.get("draft", ""), text)
        )
        if not new_draft:
            await update.message.reply_text("❌ Rephrase failed.")
            return
        comment_data["draft"] = new_draft
        ready_comments[comment_id] = comment_data
        log_id = comment_data.get("log_id")
        if log_id:
            try: log_target_comment_version(log_id, new_draft)
            except Exception as e: print(f"[bot] log error: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=message_id,
            text=_build_target_card(comment_data, "💬 Comment ready", new_draft),
            reply_markup=_comment_keyboard(comment_id),
        )
        await update.message.reply_text("✅ Rephrased. See above.")
        return

    # ── Rephrase news input ───────────────────────────────────────────────────
    if waiting_for_rephrase_news_id is not None:
        if text.lower() == "/cancel":
            waiting_for_rephrase_news_id = None
            waiting_for_rephrase_message_id = None
            await update.message.reply_text("Cancelled.")
            return
        news_id   = waiting_for_rephrase_news_id
        news_data = ready_news.get(news_id)
        if not news_data:
            waiting_for_rephrase_news_id = None
            waiting_for_rephrase_message_id = None
            await update.message.reply_text("Session expired. Run /post_news again.")
            return
        message_id = waiting_for_rephrase_message_id
        waiting_for_rephrase_news_id = None
        waiting_for_rephrase_message_id = None
        await update.message.reply_text("⏳ Rephrasing news post...")
        loop = asyncio.get_event_loop()
        new_content = await loop.run_in_executor(
            None, lambda: generate_news_post_rephrase_with_instruction(
                news_data.get("search_context", ""), news_data.get("content", ""), text)
        )
        if not new_content:
            await update.message.reply_text("❌ Rephrase failed.")
            return
        news_data["content"] = new_content
        ready_news[news_id] = news_data
        log_id = news_data.get("log_id")
        if log_id:
            try: log_news_draft_added(log_id, new_content, "rephrase_instruction")
            except Exception as e: print(f"[bot] log error: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=message_id,
            text=f"📰 Draft LinkedIn Post (rephrased):\n\n{new_content}",
            reply_markup=_news_keyboard(news_id),
        )
        await update.message.reply_text("✅ Rephrased. See above.")
        return

    # ── Target count input ────────────────────────────────────────────────────
    if waiting_for_count:
        if text.isdigit() and 1 <= int(text) <= 20:
            waiting_for_count = False
            await _begin_scan(update, context, int(text))
        else:
            await update.message.reply_text("Please send a number between 1 and 20.")


# ── Button handler ────────────────────────────────────────────────────────────
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global total_approved, is_scanning
    global waiting_for_custom_comment_id, waiting_for_custom_comment_message_id
    global waiting_for_rephrase_comment_id, waiting_for_rephrase_comment_message_id
    global waiting_for_rephrase_news_id, waiting_for_rephrase_message_id

    query = update.callback_query
    await query.answer()
    data  = query.data

    # ── Approve ───────────────────────────────────────────────────────────────
    if data.startswith("approve_"):
        target_id   = data[len("approve_"):]
        target_data = pending_targets.pop(target_id, None)
        if not target_data:
            try:
                target_data = get_pending_target(target_id)
            except Exception as e:
                print(f"[bot] get_pending_target error: {e}")
        if not target_data:
            await query.edit_message_text("⚠️ This target has expired.")
            return
        total_approved += 1
        log_id = target_data.get("log_id")
        if log_id:
            try: log_target_action(log_id, "approve")
            except Exception as e: print(f"[bot] log error: {e}")
        await query.edit_message_text(
            _build_target_card(target_data, "✅ Approved — generating comment..."),
        )
        asyncio.create_task(_prepare_comment(context.bot, target_data, query.message.message_id))

    # ── Skip ──────────────────────────────────────────────────────────────────
    elif data.startswith("skip_"):
        target_id   = data[len("skip_"):]
        target_data = pending_targets.pop(target_id, None)
        if not target_data:
            try:
                target_data = get_pending_target(target_id)
            except Exception:
                pass
        if target_data:
            log_id = target_data.get("log_id")
            if log_id:
                try:
                    log_target_action(log_id, "skip")
                    log_target_final(log_id, "skipped")
                except Exception as e: print(f"[bot] log error: {e}")
            await query.edit_message_text(
                _build_target_card(target_data, "❌ Skipped"),
            )
        else:
            await query.edit_message_text("❌ Skipped.")

    # ── Confirm (Post) ────────────────────────────────────────────────────────
    elif data.startswith("confirm_"):
        comment_id   = data[len("confirm_"):]
        comment_data = ready_comments.pop(comment_id, None)
        if not comment_data:
            await query.edit_message_text("⚠️ This comment has expired.")
            return
        log_id = comment_data.get("log_id")
        if log_id:
            try:
                log_target_action(log_id, "queued")
                log_target_final(log_id, "queued", comment_data["draft"])
            except Exception as e: print(f"[bot] log error: {e}")

        post_id = str(uuid.uuid4())[:8]
        try:
            save_pending_post(
                post_id=post_id,
                url=comment_data["url"],
                comment=comment_data["draft"],
                author_name=comment_data.get("author_name", ""),
                log_id=log_id,
                message_id=query.message.message_id,
                author_title=comment_data.get("author_title", ""),
                connection_level=comment_data.get("connection_level", ""),
                likes_count=comment_data.get("likes_count", 0),
                comments_count=comment_data.get("comments_count", 0),
                text=comment_data.get("text", ""),
                target_index=str(comment_data.get("target_index", "")),
            )
        except Exception as e:
            print(f"[bot] save_pending_post error: {e}")
            await query.edit_message_text("❌ Failed to save post data. Try again.")
            return

        if trigger_poster(post_id):
            await query.edit_message_text(
                _build_target_card(
                    comment_data,
                    "🚀 Posting via GitHub Actions (~1-2 min)...",
                    comment_data["draft"],
                ),
            )
        else:
            await query.edit_message_text("❌ Failed to trigger poster job. Check GITHUB_TOKEN.")

    # ── Regenerate ────────────────────────────────────────────────────────────
    elif data.startswith("regen_"):
        comment_id   = data[len("regen_"):]
        comment_data = ready_comments.pop(comment_id, None)
        if not comment_data:
            await query.edit_message_text("⚠️ This comment has expired.")
            return
        await query.edit_message_text(
            _build_target_card(comment_data, "🔄 Regenerating comment..."),
        )
        asyncio.create_task(_prepare_comment(context.bot, comment_data, query.message.message_id))

    # ── Custom comment ────────────────────────────────────────────────────────
    elif data.startswith("customcomment_"):
        comment_id   = data[len("customcomment_"):]
        comment_data = ready_comments.get(comment_id)
        if not comment_data:
            await query.edit_message_text("⚠️ This comment has expired.")
            return
        waiting_for_custom_comment_id         = comment_id
        waiting_for_custom_comment_message_id = query.message.message_id
        await query.edit_message_text(
            _build_target_card(comment_data, "📝 Waiting for your comment..."),
        )

    # ── Rephrase comment ──────────────────────────────────────────────────────
    elif data.startswith("repcomment_"):
        comment_id   = data[len("repcomment_"):]
        comment_data = ready_comments.get(comment_id)
        if not comment_data:
            await query.edit_message_text("⚠️ This comment has expired.")
            return
        waiting_for_rephrase_comment_id         = comment_id
        waiting_for_rephrase_comment_message_id = query.message.message_id
        await query.edit_message_text(
            _build_target_card(comment_data, "✏️ Waiting for rephrase instruction...", comment_data.get("draft")),
        )

    # ── Drop comment ──────────────────────────────────────────────────────────
    elif data.startswith("drop_"):
        comment_id   = data[len("drop_"):]
        comment_data = ready_comments.pop(comment_id, None)
        if comment_data:
            log_id = comment_data.get("log_id")
            if log_id:
                try:
                    log_target_action(log_id, "drop")
                    log_target_final(log_id, "dropped")
                except Exception as e: print(f"[bot] log error: {e}")
            await query.edit_message_text(
                _build_target_card(comment_data, "🗑 Dropped"),
            )
        else:
            await query.edit_message_text("🗑 Dropped.")

    # ── News: Post ────────────────────────────────────────────────────────────
    elif data.startswith("postnews_"):
        news_id   = data[len("postnews_"):]
        news_data = ready_news.pop(news_id, None)
        if not news_data:
            await query.edit_message_text("⚠️ This post has expired.")
            return
        content = news_data.get("content", "")
        log_id  = news_data.get("log_id")
        await query.edit_message_text("⏳ Publishing to LinkedIn...")
        asyncio.create_task(_publish_news(context.bot, content, query.message.message_id, log_id))

    # ── News: Rephrase ────────────────────────────────────────────────────────
    elif data.startswith("repnews_"):
        news_id   = data[len("repnews_"):]
        news_data = ready_news.get(news_id)
        if not news_data:
            await query.edit_message_text("⚠️ This post has expired.")
            return
        waiting_for_rephrase_news_id    = news_id
        waiting_for_rephrase_message_id = query.message.message_id
        await query.edit_message_text("✏️ Send rephrase instruction. /cancel to cancel.")

    # ── News: Fetch again ─────────────────────────────────────────────────────
    elif data.startswith("fetchnews_"):
        news_id = data[len("fetchnews_"):]
        ready_news.pop(news_id, None)
        await query.edit_message_text("🔃 Fetching fresh news...")
        asyncio.create_task(_fetch_news(context.bot, query.message.message_id))

    # ── News: Drop ────────────────────────────────────────────────────────────
    elif data.startswith("dropnews_"):
        news_id   = data[len("dropnews_"):]
        news_data = ready_news.pop(news_id, None)
        if news_data:
            log_id = news_data.get("log_id")
            if log_id:
                try: log_news_action(log_id, "drop")
                except Exception as e: print(f"[bot] log error: {e}")
        await query.edit_message_text("🗑 Dropped.")


# ── /post_news ────────────────────────────────────────────────────────────────
async def post_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global chat_id
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔍 Searching latest AI news...")
    loop       = asyncio.get_event_loop()
    result     = await loop.run_in_executor(None, generate_news_post)
    content    = result.get("content", "")
    search_ctx = result.get("search_context", "")
    if not content:
        await update.message.reply_text("❌ Failed to generate news post. Try again.")
        return
    news_id  = str(uuid.uuid4())[:8]
    tele_msg = f"📰 Draft LinkedIn Post:\n\n{content}"
    try:
        log_id = log_news_created(search_ctx, content, "fetch", tele_msg, news_id)
    except Exception as e:
        log_id = None
        print(f"[bot] log error: {e}")
    ready_news[news_id] = {"content": content, "search_context": search_ctx, "log_id": log_id}
    await update.message.reply_text(tele_msg, reply_markup=_news_keyboard(news_id))


# ── Comment preparation ───────────────────────────────────────────────────────
async def _prepare_comment(bot, target_data: dict, message_id: int):
    post_text    = target_data["text"]
    author_title = target_data.get("author_title", "Professional")
    author_name  = target_data.get("author_name", "Unknown")

    existing_comments = target_data.get("existing_comments", [])
    print(f"[bot] Using {len(existing_comments)} pre-scraped comments for {author_name}")

    loop    = asyncio.get_event_loop()
    comment = await loop.run_in_executor(
        None, lambda: generate_comment(post_text, author_title, existing_comments)
    )
    if not comment:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=_build_target_card(target_data, "❌ Failed to generate comment"),
        )
        return

    log_id = target_data.get("log_id")
    if log_id:
        try: log_target_comment_version(log_id, comment)
        except Exception as e: print(f"[bot] log error: {e}")

    comment_id = str(uuid.uuid4())[:8]
    ready_comments[comment_id] = {**target_data, "draft": comment, "log_id": log_id}

    await bot.edit_message_text(
        chat_id=chat_id, message_id=message_id,
        text=_build_target_card(ready_comments[comment_id], "💬 Comment ready", comment),
        reply_markup=_comment_keyboard(comment_id),
    )


# ── News helpers ──────────────────────────────────────────────────────────────
async def _publish_news(bot, content: str, message_id: int, log_id=None):
    loop    = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, lambda: create_post(content))
    if log_id:
        try: log_news_action(log_id, "post", content_posted=content if success else None)
        except Exception as e: print(f"[bot] log error: {e}")
    text = f"✅ Posted to LinkedIn!\n\n{content}" if success else "❌ Failed to publish."
    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)

async def _fetch_news(bot, message_id: int):
    loop       = asyncio.get_event_loop()
    result     = await loop.run_in_executor(None, generate_news_post)
    content    = result.get("content", "")
    search_ctx = result.get("search_context", "")
    if not content:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="❌ Failed to fetch.")
        return
    news_id  = str(uuid.uuid4())[:8]
    tele_msg = f"📰 Draft LinkedIn Post:\n\n{content}"
    try:
        log_id = log_news_created(search_ctx, content, "fetch", tele_msg, news_id)
    except Exception as e:
        log_id = None
        print(f"[bot] log error: {e}")
    ready_news[news_id] = {"content": content, "search_context": search_ctx, "log_id": log_id}
    await bot.edit_message_text(
        chat_id=chat_id, message_id=message_id,
        text=tele_msg, reply_markup=_news_keyboard(news_id),
    )


# ── /cancel ───────────────────────────────────────────────────────────────────
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_rephrase_news_id, waiting_for_rephrase_message_id
    global waiting_for_custom_comment_id, waiting_for_custom_comment_message_id
    global waiting_for_rephrase_comment_id, waiting_for_rephrase_comment_message_id
    waiting_for_rephrase_news_id = waiting_for_rephrase_message_id = None
    waiting_for_custom_comment_id = waiting_for_custom_comment_message_id = None
    waiting_for_rephrase_comment_id = waiting_for_rephrase_comment_message_id = None
    await update.message.reply_text("Cancelled.")


# ── /stop ─────────────────────────────────────────────────────────────────────
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_scanning, total_approved, total_posted, waiting_for_count
    global waiting_for_rephrase_news_id, waiting_for_rephrase_message_id
    global waiting_for_custom_comment_id, waiting_for_custom_comment_message_id
    global waiting_for_rephrase_comment_id, waiting_for_rephrase_comment_message_id

    is_scanning = waiting_for_count = False
    waiting_for_rephrase_news_id = waiting_for_rephrase_message_id = None
    waiting_for_custom_comment_id = waiting_for_custom_comment_message_id = None
    waiting_for_rephrase_comment_id = waiting_for_rephrase_comment_message_id = None
    pending_targets.clear()
    ready_comments.clear()
    ready_news.clear()
    total_approved = total_posted = 0
    await update.message.reply_text("🛑 Stopped. Queue cleared.\nSend /start_cron to start again.")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if os.environ.get("PORT"):
        threading.Thread(target=_start_health_server, daemon=True).start()

    app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("start_cron", start_cron))
    app.add_handler(CommandHandler("post_news",  post_news))
    app.add_handler(CommandHandler("stop",       stop))
    app.add_handler(CommandHandler("cancel",     cancel_cmd))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🤖 Bot is running. Commands: /start_cron, /post_news, /stop")
    app.run_polling()
