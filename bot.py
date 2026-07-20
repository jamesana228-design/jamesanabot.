import os
import time
import logging
import urllib.parse
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # set this in Railway variables
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "20"))  # free generations per user per day

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# In-memory usage tracker: {user_id: {"count": int, "day": "YYYY-MM-DD"}}
usage = {}

# In-memory last prompt tracker (for the "Regenerate" button): {user_id: prompt}
last_prompt = {}


def today_str():
    return time.strftime("%Y-%m-%d")


def check_and_increment_quota(user_id: int) -> bool:
    """Returns True if user is allowed to generate, False if over limit."""
    day = today_str()
    record = usage.get(user_id)
    if not record or record["day"] != day:
        record = {"count": 0, "day": day}
    if record["count"] >= DAILY_LIMIT:
        usage[user_id] = record
        return False
    record["count"] += 1
    usage[user_id] = record
    return True


def build_image_url(prompt: str, width: int = 1024, height: int = 1024, seed: int = None) -> str:
    """
    Uses Pollinations.ai — a free, no-API-key image generation endpoint.
    Docs: https://image.pollinations.ai/
    """
    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&nologo=true"
    if seed is not None:
        url += f"&seed={seed}"
    return url


def result_keyboard(prompt: str):
    # Telegram callback_data has a 64-byte limit, so we don't stuff the full prompt in it.
    # Instead we rely on last_prompt[user_id] stored server-side.
    buttons = [
        [
            InlineKeyboardButton("🔄 Regenerate", callback_data="regenerate"),
            InlineKeyboardButton("🔍 Upscale (2x)", callback_data="upscale"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------
# HANDLERS
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Hey! I'm *JamesAnaBot* — your AI image generator.\n\n"
        "Just send me a description, or use:\n"
        "`/generate a cat astronaut riding a bike`\n\n"
        f"You get {DAILY_LIMIT} free generations per day.\n\n"
        "Commands:\n"
        "/generate <prompt> — create an image\n"
        "/help — show this message again"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str, seed: int = None):
    user_id = update.effective_user.id

    if not check_and_increment_quota(user_id):
        await update.message.reply_text(
            f"⚠️ You've hit your daily limit of {DAILY_LIMIT} images. Try again tomorrow!"
        )
        return

    if not prompt or not prompt.strip():
        await update.message.reply_text(
            "Please include a description. Example:\n`/generate a dragon made of glass`",
            parse_mode="Markdown",
        )
        return

    last_prompt[user_id] = prompt

    thinking_msg = await update.message.reply_text("🎨 Generating your image...")

    try:
        image_url = build_image_url(prompt, seed=seed)
        response = requests.get(image_url, timeout=60)
        response.raise_for_status()

        await update.message.reply_photo(
            photo=response.content,
            caption=f"✨ {prompt}",
            reply_markup=result_keyboard(prompt),
        )
    except requests.RequestException as e:
        logger.error(f"Image generation failed: {e}")
        await update.message.reply_text(
            "❌ Something went wrong generating that image. Please try again."
        )
    finally:
        await thinking_msg.delete()


async def generate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args) if context.args else ""
    await generate_image(update, context, prompt)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Treat any plain text message (not a command) as a prompt."""
    prompt = update.message.text
    await generate_image(update, context, prompt)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    prompt = last_prompt.get(user_id)

    if not prompt:
        await query.message.reply_text("I don't have a previous prompt to work from. Send me a new one!")
        return

    if not check_and_increment_quota(user_id):
        await query.message.reply_text(
            f"⚠️ You've hit your daily limit of {DAILY_LIMIT} images. Try again tomorrow!"
        )
        return

    action = query.data
    thinking_msg = await query.message.reply_text("🎨 Working on it...")

    try:
        if action == "regenerate":
            seed = int(time.time())  # new seed = new variation
            image_url = build_image_url(prompt, seed=seed)
        elif action == "upscale":
            image_url = build_image_url(prompt, width=2048, height=2048)
        else:
            image_url = build_image_url(prompt)

        response = requests.get(image_url, timeout=90)
        response.raise_for_status()

        await query.message.reply_photo(
            photo=response.content,
            caption=f"✨ {prompt}",
            reply_markup=result_keyboard(prompt),
        )
    except requests.RequestException as e:
        logger.error(f"Button image generation failed: {e}")
        await query.message.reply_text("❌ Something went wrong. Please try again.")
    finally:
        await thinking_msg.delete()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("generate", generate_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.add_error_handler(error_handler)

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
