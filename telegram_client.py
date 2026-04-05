"""Telegram bot client — optional communication plugin.
Mirrors the Matrix command set. Enable with telegram.enabled in config.yaml."""
import asyncio
import logging
import json
import os
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import config
import ai
import briefing
import conversation_log
import reminders
import humanize

log = logging.getLogger("family-bot.telegram")

# Optional modules
db = None
recipes = None
buddy = None
firefly = None
email_client = None
immich_client = None

if config.GROCERY_ENABLED:
    import db
if config.RECIPES_ENABLED:
    import recipes
if config.BUDDY_ENABLED:
    import buddy
if config.FIREFLY_ENABLED:
    import firefly
if config.EMAIL_ENABLED:
    import email_client
if config.IMMICH_ENABLED:
    import immich_client

_app: Application = None


def _user_name(update: Update) -> str:
    """Get the family member name from a Telegram user ID."""
    uid = str(update.effective_user.id)
    return config.TELEGRAM_ID_TO_NAME.get(uid, update.effective_user.first_name)


def _is_owner(update: Update) -> bool:
    uid = str(update.effective_user.id)
    owner_tid = config.FAMILY_MEMBERS.get(config.OWNER, {}).get("telegram_id")
    return uid == owner_tid


def _is_private(update: Update) -> bool:
    return update.effective_chat.type == "private"


# ─── Command Handlers ───

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not db:
        return
    items = db.get_shopping_list()
    if not items:
        await update.message.reply_text("Shopping list is empty!")
        return

    by_cat = {}
    for item in items:
        cat = (item["category"] or "other").upper()
        qty = f" x{item['qty']}" if item["qty"] else ""
        by_cat.setdefault(cat, []).append(f"  {item['name']}{qty}")

    lines = ["SHOPPING LIST\n"]
    for cat, entries in sorted(by_cat.items()):
        lines.append(cat)
        lines.extend(entries)
        lines.append("")
    lines.append(f"{len(items)} items")
    await update.message.reply_text("\n".join(lines))


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not db or not ctx.args:
        await update.message.reply_text("Usage: /add <item name>")
        return
    item = " ".join(ctx.args)
    db.add_shopping_item(item, requested_by=_user_name(update))
    await update.message.reply_text(f"Added {item} to the list!")


async def cmd_bought(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not db or not ctx.args:
        await update.message.reply_text("Usage: /bought <item name>")
        return
    item = " ".join(ctx.args)
    user = _user_name(update)
    if db.mark_item_bought(item, bought_by=user):
        db.record_event(item, "bought", note=f"Bought by {user}")
        await update.message.reply_text(f"Marked {item} as bought!")
    else:
        await update.message.reply_text(f"Couldn't find '{item}' on the list.")


async def cmd_stock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not db:
        return
    items = db.get_inventory()
    if not items:
        await update.message.reply_text("No inventory data.")
        return
    by_cat = {}
    for item in items:
        cat = item["category"] or "other"
        qty = item["current_qty"] or 0
        status = "OUT" if qty == 0 else f"{qty} {item['unit']}"
        by_cat.setdefault(cat.upper(), []).append(f"  {item['name']}: {status}")
    lines = ["INVENTORY\n"]
    for cat, entries in sorted(by_cat.items()):
        lines.append(f"{cat}:")
        lines.extend(entries)
        lines.append("")
    await update.message.reply_text("\n".join(lines))


async def cmd_spent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not db or len(ctx.args) < 2:
        await update.message.reply_text("Usage: /spent <amount> <store>")
        return
    try:
        amount = float(ctx.args[0].replace("$", ""))
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return
    store = " ".join(ctx.args[1:])
    db.log_spend(store, amount)
    if firefly:
        try:
            firefly.log_receipt(store, amount)
        except Exception as e:
            log.warning(f"Firefly log failed: {e}")
    await update.message.reply_text(f"Logged ${amount:.2f} at {store}")


async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not firefly:
        await update.message.reply_text("Finance tracking is not configured.")
        return
    try:
        data = firefly.get_monthly_summary()
        lines = [f"SPENDING THIS MONTH: ${data['total']:.2f}\n"]
        for cat, amt in sorted(data["by_category"].items(), key=lambda x: -x[1]):
            lines.append(f"  {cat}: ${amt:.2f}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text("Couldn't fetch spending data.")


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not firefly:
        await update.message.reply_text("Finance tracking is not configured.")
        return
    try:
        balances = firefly.get_account_balances()
        lines = ["ACCOUNT BALANCES\n"]
        for b in balances:
            lines.append(f"  {b['name']}: ${float(b['balance']):,.2f} {b['currency']}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text("Couldn't fetch balances.")


async def cmd_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not email_client:
        await update.message.reply_text("Email is not configured.")
        return
    if not _is_owner(update):
        await update.message.reply_text("Email access is owner-only.")
        return
    try:
        emails = email_client.get_unread(limit=5)
        if not emails:
            await update.message.reply_text("Inbox clear — no unread emails.")
            return
        lines = [f"UNREAD EMAILS ({len(emails)})\n"]
        for e in emails:
            lines.append(f"From: {e['from']}")
            lines.append(f"Subject: {e['subject']}")
            lines.append(f"Date: {e['date']}")
            lines.append("")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text("Couldn't check email right now.")


async def cmd_bills(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not email_client:
        await update.message.reply_text("Email is not configured.")
        return
    if not _is_owner(update):
        await update.message.reply_text("Bill access is owner-only.")
        return
    try:
        bills = email_client.get_bills(limit=5)
        if not bills:
            await update.message.reply_text("No bills found.")
            return
        lines = ["RECENT BILLS\n"]
        for b in bills:
            lines.append(f"From: {b['from']}")
            lines.append(f"Subject: {b['subject']}")
            lines.append("")
        await update.message.reply_text("\n".join(lines))
    except Exception:
        await update.message.reply_text("Couldn't check bills.")


async def cmd_remind(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Usage: /remind <when> <what>\n"
            "Examples:\n"
            "  /remind in 30 minutes check the oven\n"
            "  /remind tomorrow call the dentist"
        )
        return
    args = " ".join(ctx.args)
    user = _user_name(update)
    sender = str(update.effective_user.id)
    remind_at, message = reminders.parse_reminder_time(args)
    if not remind_at:
        await update.message.reply_text("Couldn't figure out the time. Try: /remind in 30 minutes check the oven")
        return
    if not message:
        message = args
    reminders.add_reminder(user, sender, message, remind_at)
    await update.message.reply_text(
        f"Got it! I'll remind you: \"{message}\"\n"
        f"When: {remind_at.strftime('%B %d at %I:%M %p')}"
    )


async def cmd_reminders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = _user_name(update)
    pending = reminders.get_pending_for_user(user)
    if not pending:
        await update.message.reply_text("No pending reminders!")
        return
    lines = ["YOUR REMINDERS\n"]
    for r in pending:
        lines.append(f"  #{r['id']} - {r['message']}")
        lines.append(f"    When: {r['remind_at']}")
        lines.append("")
    lines.append("Cancel with /cancel <number>")
    await update.message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /cancel <reminder number>")
        return
    try:
        rid = int(ctx.args[0].replace("#", ""))
    except ValueError:
        await update.message.reply_text("Give me the reminder number.")
        return
    if reminders.cancel_reminder(rid):
        await update.message.reply_text(f"Reminder #{rid} cancelled.")
    else:
        await update.message.reply_text(f"Couldn't find reminder #{rid}.")


async def cmd_recipes_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not recipes:
        return
    if ctx.args:
        results = recipes.search_recipes(" ".join(ctx.args))
        if not results:
            await update.message.reply_text("No recipes found.")
            return
        lines = ["RECIPES:\n"]
        for r in results:
            lines.append(f"  #{r['id']} {r['name']}")
        lines.append("\nUse /recipe <number> for details.")
        await update.message.reply_text("\n".join(lines))
    else:
        all_recipes = recipes.list_recipes()
        if not all_recipes:
            await update.message.reply_text("No recipes saved yet.")
            return
        lines = ["ALL RECIPES:\n"]
        for r in all_recipes:
            lines.append(f"  #{r['id']} {r['name']}")
        await update.message.reply_text("\n".join(lines))


async def cmd_recipe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not recipes or not ctx.args:
        await update.message.reply_text("Usage: /recipe <number>")
        return
    try:
        rid = int(ctx.args[0].replace("#", ""))
    except ValueError:
        await update.message.reply_text("Give me the recipe number.")
        return
    data = recipes.get_recipe(rid)
    if not data:
        await update.message.reply_text(f"Recipe #{rid} not found.")
        return
    await update.message.reply_text(recipes.format_recipe(data))


async def cmd_photos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not immich_client:
        await update.message.reply_text("Photo library is not configured.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /photos <search>")
        return
    query = " ".join(ctx.args)
    results = immich_client.search_photos(query, limit=5)
    await update.message.reply_text(immich_client.format_results(results, query))
    if results:
        path = immich_client.download_thumbnail(results[0]["id"])
        if path:
            with open(path, "rb") as f:
                await update.message.reply_photo(f, caption=f"Top result for '{query}'")


async def cmd_buddy_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not buddy:
        return
    user = _user_name(update)
    if ctx.args:
        result = buddy.name_buddy(user, " ".join(ctx.args))
        await update.message.reply_text(result)
    else:
        info = buddy.format_buddy(user)
        await update.message.reply_text(info)


async def cmd_briefing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = briefing.build_briefing()
    await update.message.reply_text(msg)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sections = [f"Hey! I'm {config.BOT_NAME}. Here's what I can do:\n"]

    if config.GROCERY_ENABLED:
        sections.append(
            "GROCERIES\n"
            "/list - Shopping list\n"
            "/add <item> - Add to list\n"
            "/bought <item> - Mark as bought\n"
            "/stock - Full inventory\n"
            "/spent <amount> <store> - Log purchase"
        )
    if config.FIREFLY_ENABLED:
        sections.append("/summary - Monthly spending\n/balance - Account balances")
    sections.append(
        "REMINDERS\n"
        "/remind <when> <what> - Set a reminder\n"
        "/reminders - My pending reminders\n"
        "/cancel <#> - Cancel a reminder"
    )
    if config.RECIPES_ENABLED:
        sections.append("/recipes - Browse recipes\n/recipe <#> - Full recipe")
    if config.EMAIL_ENABLED:
        sections.append("/inbox - Check emails\n/bills - Recent bills")
    if config.IMMICH_ENABLED:
        sections.append("/photos <search> - Search family photos")
    if config.BUDDY_ENABLED:
        sections.append("/buddy - Your companion pet")
    sections.append("/briefing - Morning briefing\n/help - This message\n\nOr just talk to me naturally.")

    await update.message.reply_text("\n\n".join(sections))


# ─── Natural Language Handler ───

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle non-command messages with AI."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text:
        return

    user_name = _user_name(update)
    is_dm = _is_private(update)
    chat_id = f"tg_{update.effective_chat.id}"

    # In groups, only respond if mentioned or replied to
    if not is_dm:
        bot_username = (await ctx.bot.get_me()).username
        mentioned = f"@{bot_username}" in text
        replied = (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user
            and update.message.reply_to_message.from_user.is_bot
        )
        if not mentioned and not replied:
            return
        text = text.replace(f"@{bot_username}", "").strip()

    result = ai.handle_message(text, user_name=user_name, is_private=is_dm, chat_id=chat_id)
    reply = result["reply"]
    actions = result.get("actions", [])

    if reply:
        await update.message.reply_text(reply)
        conversation_log.log_interaction(user_name, text, reply)

    # Process actions
    for action in actions:
        act = action.get("action")
        item = action.get("item", "")
        if act == "add" and item and db:
            db.add_shopping_item(item, requested_by=user_name)
        elif act == "bought" and item and db:
            db.mark_item_bought(item, bought_by=user_name)
        elif act == "remove" and item and db:
            db.remove_shopping_item(item)
        elif act == "remind":
            remind_text = action.get("time", "") + " " + action.get("message", "")
            remind_at, remind_msg = reminders.parse_reminder_time(remind_text)
            if remind_at and remind_msg:
                reminders.add_reminder(user_name, str(update.effective_user.id), remind_msg, remind_at)
        elif act == "log_spend" and db:
            store = action.get("store", "Unknown")
            amount = action.get("amount", 0)
            if amount:
                db.log_spend(store, float(amount))
                if firefly:
                    try:
                        firefly.log_receipt(store, float(amount))
                    except Exception:
                        pass
        elif act == "search_photos" and immich_client:
            query = action.get("query", "")
            if query:
                results = immich_client.search_photos(query, limit=3)
                if results:
                    path = immich_client.download_thumbnail(results[0]["id"])
                    if path:
                        with open(path, "rb") as f:
                            await update.message.reply_photo(f, caption=f"Photo: {query}")


# ─── Send helpers (for scheduled jobs) ───

async def send_to_group(text: str):
    """Send a message to the family Telegram group."""
    if _app and config.FAMILY_GROUP_ID:
        await _app.bot.send_message(chat_id=config.FAMILY_GROUP_ID, text=text)


async def send_to_owner(text: str):
    """DM the owner."""
    owner_tid = config.FAMILY_MEMBERS.get(config.OWNER, {}).get("telegram_id")
    if _app and owner_tid:
        await _app.bot.send_message(chat_id=owner_tid, text=text)


# ─── Startup ───

def build_app() -> Application:
    """Build the Telegram application with all handlers."""
    global _app

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        token_file = config._CONFIG_DIR / "telegram_token.txt"
        if token_file.exists():
            token = token_file.read_text().strip()

    if not token:
        log.error("No Telegram bot token found. Set TELEGRAM_BOT_TOKEN env var.")
        return None

    _app = Application.builder().token(token).build()

    # Always-on commands
    _app.add_handler(CommandHandler("remind", cmd_remind))
    _app.add_handler(CommandHandler("reminders", cmd_reminders))
    _app.add_handler(CommandHandler("cancel", cmd_cancel))
    _app.add_handler(CommandHandler("briefing", cmd_briefing))
    _app.add_handler(CommandHandler("help", cmd_help))
    _app.add_handler(CommandHandler("start", cmd_help))

    # Grocery
    if config.GROCERY_ENABLED:
        _app.add_handler(CommandHandler("list", cmd_list))
        _app.add_handler(CommandHandler("add", cmd_add))
        _app.add_handler(CommandHandler("bought", cmd_bought))
        _app.add_handler(CommandHandler("stock", cmd_stock))
        _app.add_handler(CommandHandler("spent", cmd_spent))

    # Finance
    if config.FIREFLY_ENABLED:
        _app.add_handler(CommandHandler("summary", cmd_summary))
        _app.add_handler(CommandHandler("balance", cmd_balance))

    # Email
    if config.EMAIL_ENABLED:
        _app.add_handler(CommandHandler("inbox", cmd_inbox))
        _app.add_handler(CommandHandler("bills", cmd_bills))

    # Recipes
    if config.RECIPES_ENABLED:
        _app.add_handler(CommandHandler("recipes", cmd_recipes_list))
        _app.add_handler(CommandHandler("recipe", cmd_recipe))

    # Photos
    if config.IMMICH_ENABLED:
        _app.add_handler(CommandHandler("photos", cmd_photos))

    # Buddy
    if config.BUDDY_ENABLED:
        _app.add_handler(CommandHandler("buddy", cmd_buddy_handler))

    # Natural language (catch-all, must be last)
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    return _app


async def start():
    """Start the Telegram bot (non-blocking)."""
    app = build_app()
    if not app:
        log.warning("Telegram disabled — no bot token")
        return

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram bot started")


async def stop():
    """Stop the Telegram bot."""
    if _app:
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
        log.info("Telegram bot stopped")
