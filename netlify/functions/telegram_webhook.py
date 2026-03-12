import os, json
from http import HTTPStatus

from telegram import Bot
from ai import generate_news_post

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN must be set in environment")

bot = Bot(token=TELEGRAM_TOKEN)

HELP_TEXT = """LinkedIn Bot (Netlify function)
Available commands:
/start_cron - not supported in serverless (requires long-running process)
/post_news - generate AI news draft and return it in chat
/stop - not supported in serverless
/help - this message
"""


def handler(event, context):
    try:
        body = json.loads(event.get("body", "{}"))
    except Exception as e:
        return {"statusCode": HTTPStatus.BAD_REQUEST, "body": "invalid JSON"}

    if not body:
        return {"statusCode": HTTPStatus.BAD_REQUEST, "body": "no body"}

    message = body.get("message") or body.get("edited_message")
    if not message:
        # nothing to do for unsupported update types
        return {"statusCode": HTTPStatus.OK, "body": "ok"}

    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return {"statusCode": HTTPStatus.OK, "body": "skipped"}

    if text.lower().startswith("/start_cron"):
        bot.send_message(chat_id=chat_id, text="⚠️ /start_cron is not available in this serverless environment. Please deploy the full bot on a VM or container to run feed scanning.")
        return {"statusCode": HTTPStatus.OK, "body": "ok"}

    if text.lower().startswith("/post_news"):
        data = generate_news_post()
        content = data.get("content") or "(news generation returned empty)"
        bot.send_message(chat_id=chat_id, text=f"📰 Generated news draft:\n\n{content}")
        return {"statusCode": HTTPStatus.OK, "body": "ok"}

    if text.lower().startswith("/stop"):
        bot.send_message(chat_id=chat_id, text="⚠️ /stop is not supported in serverless mode.")
        return {"statusCode": HTTPStatus.OK, "body": "ok"}

    if text.lower().startswith("/help"):
        bot.send_message(chat_id=chat_id, text=HELP_TEXT)
        return {"statusCode": HTTPStatus.OK, "body": "ok"}

    bot.send_message(chat_id=chat_id, text="Unsupported command in this Netlify function. Use /help.")
    return {"statusCode": HTTPStatus.OK, "body": "ok"}
