"""
Telegram Bot entry point for the Pixel 10 Pro Google One Gemini Bot.

Commands:
  /start        – Show welcome message and available commands
  /login        – Begin credential capture flow (email → password)
  /check_offer  – Run Google One automation and look for Gemini Pro offer
  /get_link     – Show the last captured offer link
  /status       – Show current session status and device profile
"""

import asyncio
import logging
import os
import sys

import telegram
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
from device_simulator import create_device_profile
from google_automation import check_gemini_offer, GoogleAutomationError

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    """Ensure polling starts without a webhook or stale Telegram updates."""
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Telegram webhook deleted; polling startup is ready")
    except Exception:
        logger.exception("Webhook cleanup failed; polling will continue")


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle telegram errors — especially 409 Conflict."""
    from telegram.error import Conflict, NetworkError
    err = context.error
    if isinstance(err, Conflict):
        logger.warning("409 Conflict detected — waiting 15s for other instance to die...")
        await asyncio.sleep(15)
    elif isinstance(err, NetworkError):
        logger.warning("Network error: %s — retrying...", err)
    else:
        logger.exception("Unhandled error: %s", err, exc_info=err)

# ── Conversation states ───────────────────────────────────────────────────────
AWAIT_EMAIL, AWAIT_PASSWORD, AWAIT_TOTP = range(3)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_session(chat_id: int) -> dict:
    """Return (creating if absent) the session dict for *chat_id*."""
    if chat_id not in config.SESSION_STORE:
        config.SESSION_STORE[chat_id] = {}
    return config.SESSION_STORE[chat_id]


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message with command menu."""
    await update.message.reply_text(
        "🤖 *Pixel 10 Pro Google One Bot*\n\n"
        "This bot simulates a Google Pixel 10 Pro (Android 16) device, "
        "logs into your Google account, and retrieves the *12-month free "
        "Gemini Pro* offer link from Google One.\n\n"
        "📋 *Available Commands:*\n"
        "• /login – Enter your Gmail credentials\n"
        "• /check\\_offer – Detect the Gemini Pro offer\n"
        "• /get\\_link – Show the last captured offer link\n"
        "• /status – View current session & device info\n\n"
        "⚠️ *Privacy Note:* Credentials are held in memory only for the "
        "duration of the session and never stored persistently.",
        parse_mode="Markdown",
    )


# ── /login conversation ───────────────────────────────────────────────────────

async def login_start(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin the login conversation – ask for email."""
    await update.message.reply_text(
        "📧 Please enter your Gmail address:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AWAIT_EMAIL


async def login_email(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the email and ask for password."""
    email = update.message.text.strip()
    context.user_data["pending_email"] = email
    await update.message.reply_text(
        f"✅ Email received: `{email}`\n\n🔒 Now enter your password:",
        parse_mode="Markdown",
    )
    return AWAIT_PASSWORD


async def login_password(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store password, then ask for TOTP key (optional)."""
    password = update.message.text.strip()
    context.user_data["pending_password"] = password

    # Delete the message containing the password for security
    try:
        await update.message.delete()
    except Exception:
        pass

    await update.message.reply_text(
        "🔐 Password saved.\n\n"
        "Now enter your *TOTP key* (from Google Authenticator setup).\n"
        "This is the 32-character secret key.\n\n"
        "_Type `skip` if you don't have 2FA enabled._",
        parse_mode="Markdown",
    )
    return AWAIT_TOTP


async def login_totp(update: Update,
                     context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store TOTP key, create device profile, and finish."""
    chat_id = update.effective_chat.id
    totp_input = update.message.text.strip()
    email = context.user_data.pop("pending_email", "")
    password = context.user_data.pop("pending_password", "")

    session = _get_session(chat_id)
    session["email"] = email
    session["password"] = password
    session["device"] = create_device_profile()
    session["offer_link"] = None

    if totp_input.lower() == "skip":
        session["totp_key"] = ""
        totp_msg = "❌ No 2FA"
    else:
        # Clean up: remove spaces, take only valid base32 chars
        clean = "".join(c.upper() for c in totp_input if c.isalnum())
        session["totp_key"] = clean
        totp_msg = "✅ 2FA enabled"

    # Delete TOTP message for security
    try:
        await update.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ *Credentials saved* and a new Pixel 10 Pro device profile has "
            "been created for this session.\n\n"
            + totp_msg + "\n\n"
            + session["device"].summary()
            + "\n\nUse /check\\_offer to search for the Gemini Pro offer."
        ),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def login_cancel(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the login conversation."""
    context.user_data.pop("pending_email", None)
    await update.message.reply_text(
        "❌ Login cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── /check_offer ──────────────────────────────────────────────────────────────

async def check_offer(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run Google One automation and report the result."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if not session.get("email") or not session.get("password"):
        await update.message.reply_text(
            "⚠️ No credentials found. Please use /login first."
        )
        return

    device = session.get("device")
    if not device:
        device = create_device_profile()
        session["device"] = device

    await update.message.reply_text(
        "⏳ Launching Pixel 10 Pro emulator…\n"
        "Logging into Google + searching for Gemini Pro offer.\n"
        "_This may take up to 90 seconds._"
    )

    try:
        offer_link = check_gemini_offer(
            session["email"],
            session["password"],
            device,
            session.get("totp_key", ""),
        )
    except GoogleAutomationError as exc:
        await update.message.reply_text(f"❌ *Error:* {exc}", parse_mode="Markdown")
        return
    except Exception as exc:
        logger.exception("Unexpected error in check_offer for chat %s", chat_id)
        import traceback
        tb = traceback.format_exc()
        await update.message.reply_text(
            f"❌ Error: {exc}\n\n```\n{tb[-500:]}\n```",
            parse_mode="Markdown"
        )
        return

    if offer_link:
        session["offer_link"] = offer_link
        await update.message.reply_text(
            "🎉 Gemini Pro Offer Found!\n\n"
            "Click the link below to activate your 12-month free Gemini Pro:\n\n"
            f"{offer_link}\n\n"
            "Use /get_link to retrieve this link again."
        )
    else:
        await update.message.reply_text(
            "😔 No active Gemini Pro offer was detected on your Google One "
            "account at this time.\n\n"
            "The offer may not be available for your account region or may "
            "have already been activated. Try again later."
        )


# ── /get_link ─────────────────────────────────────────────────────────────────

async def get_link(update: Update,
                   context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the last captured offer link for this session."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)
    link = session.get("offer_link")

    if link:
        await update.message.reply_text(
            f"🔗 *Last captured offer link:*\n\n{link}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "ℹ️ No offer link has been captured yet. "
            "Use /check\\_offer to search for the Gemini Pro offer.",
            parse_mode="Markdown",
        )


# ── /status ───────────────────────────────────────────────────────────────────

async def status(update: Update,
                 context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current session and device profile summary."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if not session:
        await update.message.reply_text(
            "ℹ️ No active session. Use /login to get started."
        )
        return

    email = session.get("email", "—")
    has_creds = bool(session.get("email") and session.get("password"))
    has_totp = bool(session.get("totp_key"))
    offer_link = session.get("offer_link")
    device = session.get("device")

    lines = [
        "📊 *Session Status*\n",
        f"Account: `{email}`",
        f"Credentials loaded: {'✅' if has_creds else '❌'}",
        f"2FA (TOTP): {'✅' if has_totp else '❌'}",
        f"Offer link captured: {'✅' if offer_link else '❌'}",
    ]

    if device:
        lines.append("\n" + device.summary())

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


# ── Application setup ─────────────────────────────────────────────────────────

def main() -> None:
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN environment variable is not set. "
            "Set it in Railway Variables and restart."
        )
        sys.exit(1)

    # Delete any webhook in the async PTB lifecycle before polling starts.
    # This avoids a blocking HTTP call and keeps all Telegram I/O inside PTB.
    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .build()
    )
    app.add_error_handler(_error_handler)

    # /login conversation
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            AWAIT_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_email)
            ],
            AWAIT_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)
            ],
            AWAIT_TOTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_totp)
            ],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(login_conv)
    app.add_handler(CommandHandler("check_offer", check_offer))
    app.add_handler(CommandHandler("get_link", get_link))
    app.add_handler(CommandHandler("status", status))

    logger.info("Bot is running. Press Ctrl-C to stop.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
