#!/usr/bin/env python3
"""
Family Bot - Grocery, finance, and household management for the family.
Runs as a standalone service on the Pi alongside OpenClaw.
"""
import asyncio
import logging
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
import db
import ai
import briefing
import firefly
import email_client
import voice_api
import conversation_log
import mem0_memory
import reminders
import recipes
import matrix_client
import humanize

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path.home() / "family-bot.log"),
    ],
)
log = logging.getLogger("family-bot")


# ─── Telegram Command Handlers ───

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current shopping list — formatted for easy reading in-store."""
    items = db.get_shopping_list()
    if not items:
        await update.message.reply_text("Shopping list is empty! Nothing to buy.")
        return

    cat_emoji = {
        "PRODUCE": "🥬", "DAIRY": "🥛", "MEAT": "🥩", "BAKERY": "🍞",
        "PANTRY": "🥫", "FROZEN": "🧊", "HOUSEHOLD": "🧹", "PET": "🐕",
        "OTHER": "📦",
    }

    by_cat = {}
    for item in items:
        cat = (item["category"] or "other").upper()
        qty = f"  ×{item['qty']}" if item["qty"] else ""
        by_cat.setdefault(cat, []).append(f"▫️  {item['name']}{qty}")

    lines = ["<b>🛒 SHOPPING LIST</b>\n"]
    for cat, entries in sorted(by_cat.items()):
        emoji = cat_emoji.get(cat, "📦")
        lines.append(f"<b>{emoji} {cat}</b>")
        lines.extend(entries)
        lines.append("")

    lines.append(f"<b>{len(items)} items</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add item to shopping list."""
    if not context.args:
        await update.message.reply_text("Usage: /add <item name>")
        return

    item_name = " ".join(context.args)
    user_id = str(update.effective_user.id)
    requested_by = config.TELEGRAM_ID_TO_NAME.get(user_id, update.effective_user.first_name)

    db.add_shopping_item(item_name, requested_by=requested_by)
    await update.message.reply_text(f"Added {item_name} to the list!")


async def cmd_bought(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mark item as bought."""
    if not context.args:
        await update.message.reply_text("Usage: /bought <item name>")
        return

    item_name = " ".join(context.args)
    user_id = str(update.effective_user.id)
    bought_by = config.TELEGRAM_ID_TO_NAME.get(user_id, update.effective_user.first_name)

    if db.mark_item_bought(item_name, bought_by=bought_by):
        db.record_event(item_name, "bought", note=f"Bought by {bought_by}")
        await update.message.reply_text(f"Marked {item_name} as bought!")
    else:
        await update.message.reply_text(f"Couldn't find '{item_name}' on the shopping list.")


async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show inventory status."""
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


async def cmd_spent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log a spend. Usage: /spent <amount> <store>"""
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /spent <amount> <store>\nExample: /spent 45.50 Fortinos")
        return

    try:
        amount = float(context.args[0].replace("$", ""))
    except ValueError:
        await update.message.reply_text("Amount must be a number. Example: /spent 45.50 Fortinos")
        return

    store = " ".join(context.args[1:])
    entry = db.log_spend(store, amount)
    # Also log to Firefly III
    try:
        firefly.log_receipt(store, amount)
    except Exception as e:
        log.warning(f"Firefly log failed: {e}")
    await update.message.reply_text(f"Logged ${amount:.2f} at {store}")


async def cmd_owe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show outstanding payments."""
    lines = []
    for tracker in config.WORKSPACE.glob("*_payment_tracker.json"):
        try:
            with open(tracker) as f:
                data = json.load(f)
            if data.get("status") == "pending":
                lines.append(
                    f"- {data['debtor']} owes {data['creditor']} ${data['amount']:.2f} "
                    f"({data.get('purpose', 'unknown')})"
                )
        except (json.JSONDecodeError, KeyError):
            continue
    if lines:
        await update.message.reply_text("OUTSTANDING PAYMENTS:\n\n" + "\n".join(lines))
    else:
        await update.message.reply_text("No outstanding payments!")


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Monthly spending summary from Firefly III."""
    try:
        data = firefly.get_monthly_summary()
        lines = [f"SPENDING THIS MONTH: ${data['total']:.2f}\n"]
        for cat, amt in sorted(data["by_category"].items(), key=lambda x: -x[1]):
            lines.append(f"  {cat}: ${amt:.2f}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        log.error(f"Firefly summary error: {e}")
        await update.message.reply_text("Couldn't fetch spending data right now.")


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show account balances from Firefly III. DM only (private data)."""
    if update.effective_chat.type != "private":
        await update.message.reply_text("I'll DM you the balances — that's private info.")
        user_id = str(update.effective_user.id)
        try:
            balances = firefly.get_account_balances()
            lines = ["ACCOUNT BALANCES\n"]
            for b in balances:
                lines.append(f"  {b['name']}: ${float(b['balance']):,.2f} {b['currency']}")
            await context.bot.send_message(chat_id=user_id, text="\n".join(lines))
        except Exception as e:
            log.error(f"Firefly balance error: {e}")
            await context.bot.send_message(chat_id=user_id, text="Couldn't fetch balances right now.")
        return
    try:
        balances = firefly.get_account_balances()
        lines = ["ACCOUNT BALANCES\n"]
        for b in balances:
            lines.append(f"  {b['name']}: ${float(b['balance']):,.2f} {b['currency']}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        log.error(f"Firefly balance error: {e}")
        await update.message.reply_text("Couldn't fetch balances right now.")


async def cmd_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check unread emails. DM only."""
    user_id = str(update.effective_user.id)
    if user_id != config.FAMILY_MEMBERS[config.OWNER].get("telegram_id"):
        await update.message.reply_text("Email access is owner-only.")
        return

    reply_chat = update.effective_chat.id
    if update.effective_chat.type != "private":
        await update.message.reply_text("I'll DM you — email is private.")
        reply_chat = user_id

    try:
        emails = email_client.get_unread(limit=5)
        if not emails:
            await context.bot.send_message(chat_id=reply_chat, text="Inbox is clear — no unread emails.")
            return

        lines = [f"UNREAD EMAILS ({len(emails)})\n"]
        for e in emails:
            lines.append(f"From: {e['from']}")
            lines.append(f"Subject: {e['subject']}")
            lines.append(f"Date: {e['date']}")
            lines.append("")

        await context.bot.send_message(chat_id=reply_chat, text="\n".join(lines))
    except Exception as e:
        log.error(f"Email error: {e}")
        await context.bot.send_message(chat_id=reply_chat, text="Couldn't check email right now.")


async def cmd_bills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check recent bills from email. DM only."""
    user_id = str(update.effective_user.id)
    if user_id != config.FAMILY_MEMBERS[config.OWNER].get("telegram_id"):
        await update.message.reply_text("Bill access is owner-only.")
        return

    reply_chat = update.effective_chat.id
    if update.effective_chat.type != "private":
        await update.message.reply_text("I'll DM you the bills.")
        reply_chat = user_id

    try:
        bills = email_client.get_bills(limit=5)
        if not bills:
            await context.bot.send_message(chat_id=reply_chat, text="No bills found.")
            return

        lines = ["RECENT BILLS\n"]
        for b in bills:
            lines.append(f"From: {b['from']}")
            lines.append(f"Subject: {b['subject']}")
            lines.append(f"Date: {b['date']}")
            # Try to parse amount
            parsed = email_client.parse_bill_email(b["subject"], b["body_preview"], b["from"])
            if parsed and parsed.get("amount"):
                lines.append(f"Amount: ${parsed['amount']:.2f}")
                if parsed.get("due_date"):
                    lines.append(f"Due: {parsed['due_date']}")
            lines.append("")

        await context.bot.send_message(chat_id=reply_chat, text="\n".join(lines))
    except Exception as e:
        log.error(f"Bills error: {e}")
        await context.bot.send_message(chat_id=reply_chat, text="Couldn't check bills right now.")


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set a reminder. Usage: /remind <time> <message>"""
    if not context.args:
        await update.message.reply_text(
            "Usage: /remind <when> <what>\n"
            "Examples:\n"
            "  /remind in 30 minutes check the oven\n"
            "  /remind tomorrow call the dentist\n"
            "  /remind at 3pm pick up Emily"
        )
        return

    text = " ".join(context.args)
    user_id = str(update.effective_user.id)
    user_name = config.TELEGRAM_ID_TO_NAME.get(user_id, update.effective_user.first_name)

    remind_at, message = reminders.parse_reminder_time(text)
    if not remind_at:
        await update.message.reply_text("I couldn't figure out the time. Try: /remind in 30 minutes check the oven")
        return

    # Clean up the message
    message = message.strip()
    for prefix in ("to ", "that ", "me to ", "me that ", "remind me to ", "remind me "):
        if message.lower().startswith(prefix):
            message = message[len(prefix):]
            break

    if not message:
        message = text

    reminders.add_reminder(user_name, user_id, message, remind_at)
    await update.message.reply_text(
        f"Got it! I'll remind you: \"{message}\"\n"
        f"When: {remind_at.strftime('%B %d at %I:%M %p')}"
    )


async def cmd_my_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending reminders."""
    user_id = str(update.effective_user.id)
    user_name = config.TELEGRAM_ID_TO_NAME.get(user_id, update.effective_user.first_name)

    pending = reminders.get_pending_for_user(user_name)
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


async def cmd_cancel_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel a reminder by ID."""
    if not context.args:
        await update.message.reply_text("Usage: /cancel <reminder number>")
        return
    try:
        rid = int(context.args[0].replace("#", ""))
    except ValueError:
        await update.message.reply_text("Give me the reminder number, e.g. /cancel 3")
        return

    if reminders.cancel_reminder(rid):
        await update.message.reply_text(f"Reminder #{rid} cancelled.")
    else:
        await update.message.reply_text(f"Couldn't find reminder #{rid}.")


async def cmd_recipes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List or search recipes."""
    if context.args:
        query = " ".join(context.args)
        results = recipes.search_recipes(query)
        if not results:
            await update.message.reply_text(f"No recipes found for '{query}'.")
            return
        lines = [f"RECIPES matching '{query}':\n"]
        for r in results:
            time_str = ""
            total = (r["prep_time"] or 0) + (r["cook_time"] or 0)
            if total:
                time_str = f" ({total} min)"
            lines.append(f"  #{r['id']} {r['name']}{time_str}")
        lines.append("\nUse /recipe <number> to see the full recipe.")
        await update.message.reply_text("\n".join(lines))
    else:
        all_recipes = recipes.list_recipes()
        if not all_recipes:
            await update.message.reply_text("No recipes saved yet.")
            return
        lines = ["ALL RECIPES:\n"]
        for r in all_recipes:
            time_str = ""
            total = (r["prep_time"] or 0) + (r["cook_time"] or 0)
            if total:
                time_str = f" ({total} min)"
            lines.append(f"  #{r['id']} {r['name']}{time_str}")
        lines.append("\nUse /recipe <number> for details, or /recipes <search> to search.")
        await update.message.reply_text("\n".join(lines))


async def cmd_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a specific recipe by ID."""
    if not context.args:
        await update.message.reply_text("Usage: /recipe <number>")
        return
    try:
        rid = int(context.args[0].replace("#", ""))
    except ValueError:
        await update.message.reply_text("Give me the recipe number, e.g. /recipe 1")
        return

    data = recipes.get_recipe(rid)
    if not data:
        await update.message.reply_text(f"Recipe #{rid} not found.")
        return

    formatted = recipes.format_recipe(data)
    await update.message.reply_text(formatted)


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """On-demand morning briefing."""
    msg = briefing.build_briefing()
    await update.message.reply_text(msg)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available commands."""
    await update.message.reply_text(
        "HEY! I'm Frank. Here's what I can do:\n\n"
        "GROCERIES\n"
        "/list - Shopping list\n"
        "/add <item> - Add to list\n"
        "/bought <item> - Mark as bought\n"
        "/stock - Full inventory\n\n"
        "MONEY\n"
        "/spent <amount> <store> - Log a purchase\n"
        "/summary - Monthly spending breakdown\n"
        "/balance - Account balances (DM only)\n"
        "/owe - Who owes who\n\n"
        "REMINDERS\n"
        "/remind <when> <what> - Set a reminder\n"
        "/reminders - My pending reminders\n"
        "/cancel <#> - Cancel a reminder\n\n"
        "RECIPES\n"
        "/recipes - List all recipes\n"
        "/recipes <search> - Search recipes\n"
        "/recipe <#> - Show full recipe\n\n"
        "EMAIL (DM only)\n"
        "/inbox - Unread emails\n"
        "/bills - Recent bills\n\n"
        "OTHER\n"
        "/briefing - Morning briefing\n"
        "/help - This message\n\n"
        "Or just talk to me naturally — I'm not picky about commands."
    )


# ─── Natural Language Handler ───

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-form text messages with AI."""
    text = update.message.text
    if not text:
        return

    user_id = str(update.effective_user.id)
    user_name = config.TELEGRAM_ID_TO_NAME.get(user_id, update.effective_user.first_name)

    # Quick pattern matching for common requests (no AI needed)
    lower = text.lower().strip()

    # Don't respond to short acknowledgements
    ack_words = {"ok", "okay", "k", "thanks", "thank you", "thx", "ty",
                 "cool", "got it", "sure", "yep", "yup", "np", "alright",
                 "sounds good", "perfect", "great", "nice", "good"}
    if lower.rstrip("!.,") in ack_words:
        return

    list_triggers = ("what do we need", "grocery list", "shopping list", "what's on the list",
                     "show me the list", "what do we have to buy", "what do we need to buy",
                     "what's on the grocery", "show the list", "what we need",
                     "can you show me the grocery", "send me the list", "send the list",
                     "what are we getting", "what should we get")
    if any(trigger in lower for trigger in list_triggers):
        await cmd_list(update, context)
        return

    if lower.startswith("add "):
        item = text[4:].strip()
        if item:
            db.add_shopping_item(item, requested_by=user_name)
            await update.message.reply_text(f"Added {item} to the list!")
            return

    if lower.startswith("bought ") or lower.startswith("got "):
        item = text.split(" ", 1)[1].strip()
        if item and db.mark_item_bought(item, bought_by=user_name):
            await update.message.reply_text(f"Marked {item} as bought!")
            return

    # Fall through to AI for everything else
    try:
        is_private = update.effective_chat.type == "private"
        chat_id = str(update.effective_chat.id)
        result = ai.handle_message(text, user_name=user_name, is_private=is_private, chat_id=chat_id)
        reply = result["reply"]
        actions = result.get("actions", [])

        # Execute all actions the AI decided to take
        for action in actions:
            act = action.get("action")
            item = action.get("item", "")
            if act == "add" and item:
                db.add_shopping_item(item, requested_by=user_name)
            elif act == "bought" and item:
                db.mark_item_bought(item, bought_by=user_name)
                db.record_event(item, "bought", note=f"Bought by {user_name}")
            elif act == "remove" and item:
                db.remove_shopping_item(item)
            elif act == "remind":
                remind_text = action.get("time", "") + " " + action.get("message", "")
                remind_at, remind_msg = reminders.parse_reminder_time(remind_text)
                if remind_at and remind_msg:
                    reminders.add_reminder(user_name, user_id, remind_msg, remind_at)
                elif action.get("message"):
                    # Couldn't parse time, try just the message with a default
                    reminders.add_reminder(user_name, user_id, action["message"],
                                          datetime.now() + timedelta(hours=1))
            elif act == "send_message":
                to_user = action.get("to", "").lower()
                msg_text = action.get("message", "")
                if to_user in config.FAMILY_MEMBERS and msg_text:
                    target_id = config.FAMILY_MEMBERS[to_user]["telegram_id"]
                    try:
                        await context.bot.send_message(chat_id=target_id, text=msg_text)
                        log.info(f"Message sent to {to_user}: {msg_text[:50]}")
                    except Exception as e:
                        log.error(f"Failed to send message to {to_user}: {e}")
            elif act == "log_spend":
                store = action.get("store", "Unknown")
                amount = action.get("amount", 0)
                if amount:
                    db.log_spend(store, float(amount))
                    try:
                        firefly.log_receipt(store, float(amount))
                    except Exception as e:
                        log.warning(f"Firefly log failed: {e}")

        if reply:
            await update.message.reply_text(reply)
            # Log to conversation memory (both systems)
            conversation_log.log_interaction(user_name, text, reply)
            conversation_log.extract_and_save_learnings(user_name, text, reply)
            # Mem0: auto-extract structured facts
            mem0_memory.add_conversation(user_name, text, reply)
    except Exception as e:
        log.error(f"AI error: {e}")
        await update.message.reply_text("Sorry, I couldn't process that right now. 😅")


# ─── Photo Handler (Receipt Parsing) ───

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages - attempt receipt parsing."""
    photo = update.message.photo[-1]  # highest resolution
    file = await context.bot.get_file(photo.file_id)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        user_id = str(update.effective_user.id)
        user_name = config.TELEGRAM_ID_TO_NAME.get(user_id, update.effective_user.first_name)

        await update.message.reply_text("Parsing receipt...")
        receipt = ai.parse_receipt_image(tmp_path)

        store = receipt.get("store", "Unknown")
        total = receipt.get("total", 0)
        items = receipt.get("items", [])

        db.log_spend(store, total, items)

        # Log to Firefly III
        firefly_id = None
        try:
            firefly_id = firefly.log_receipt(store, total, items)
        except Exception as e:
            log.warning(f"Firefly receipt log failed: {e}")

        item_lines = [f"  {i['name']}: ${i.get('price', '?')}" for i in items[:10]]
        msg = (
            f"Got it! Receipt from {store}\n"
            f"Total: ${total:.2f}\n\n"
            + "\n".join(item_lines)
        )
        if len(items) > 10:
            msg += f"\n  ...and {len(items) - 10} more"
        if firefly_id:
            msg += f"\n\nLogged to Firefly III"

        await update.message.reply_text(msg)
    except Exception as e:
        log.error(f"Receipt parsing error: {e}")
        await update.message.reply_text("Couldn't parse that as a receipt. Is it a clear photo of a grocery receipt?")
    finally:
        os.unlink(tmp_path)


# ─── Scheduled Jobs ───

async def job_morning_briefing(app: Application):
    """6:30 AM - Morning briefing to family group."""
    msg = briefing.build_briefing()
    await app.bot.send_message(chat_id=config.FAMILY_GROUP_ID, text=msg)
    try:
        await matrix_client.send_to_family_group(msg)
    except Exception as e:
        log.warning(f"Matrix briefing failed: {e}")
    log.info("Morning briefing sent")


async def job_grocery_push(app: Application):
    """9:00 AM - Push shopping list to family group."""
    items = db.get_shopping_list()
    if not items:
        return  # nothing to push

    cat_emoji = {
        "PRODUCE": "🥬", "DAIRY": "🥛", "MEAT": "🥩", "BAKERY": "🍞",
        "PANTRY": "🥫", "FROZEN": "🧊", "HOUSEHOLD": "🧹", "PET": "🐕",
        "OTHER": "📦",
    }

    by_cat = {}
    for item in items:
        cat = (item["category"] or "other").upper()
        by_cat.setdefault(cat, []).append(f"▫️  {item['name']}")

    lines = ["<b>🛒 SHOPPING LIST</b>\n"]
    for cat, entries in sorted(by_cat.items()):
        emoji = cat_emoji.get(cat, "📦")
        lines.append(f"<b>{emoji} {cat}</b>")
        lines.extend(entries)
        lines.append("")
    lines.append(f"<b>{len(items)} items</b> — reply /bought &lt;item&gt; when you pick something up!")

    await app.bot.send_message(chat_id=config.FAMILY_GROUP_ID, text="\n".join(lines), parse_mode="HTML")
    # Also push to Matrix (plain text version)
    plain_lines = [l.replace("<b>", "").replace("</b>", "").replace("&lt;", "<").replace("&gt;", ">") for l in lines]
    try:
        await matrix_client.send_to_family_group("\n".join(plain_lines))
    except Exception as e:
        log.warning(f"Matrix grocery push failed: {e}")
    log.info(f"Grocery list pushed ({len(items)} items)")


async def job_low_stock_alert(app: Application):
    """6:00 PM - Alert about low stock items."""
    low = db.get_low_stock_items()
    alerts = db.get_consumption_alerts()

    if not low and not alerts:
        return

    lines = ["LOW STOCK ALERT\n"]
    if low:
        lines.append("Out of stock:")
        for item in low:
            lines.append(f"  - {item['name']}")
    if alerts:
        lines.append("\nRunning low (based on usage):")
        for item in alerts:
            lines.append(f"  - {item['name']} (last bought {int(item['days_since'])} days ago)")

    await app.bot.send_message(chat_id=config.FAMILY_GROUP_ID, text="\n".join(lines))
    try:
        await matrix_client.send_to_family_group("\n".join(lines))
    except Exception as e:
        log.warning(f"Matrix low stock alert failed: {e}")
    log.info("Low stock alert sent")


async def job_bill_scan(app: Application):
    """8:00 AM - Scan Bills folder for new bills, log to Firefly if parseable."""
    owner_id = config.FAMILY_MEMBERS[config.OWNER]["telegram_id"]
    try:
        bills = email_client.get_bills(limit=5)
        new_bills = []
        for b in bills:
            parsed = email_client.parse_bill_email(b["subject"], b["body_preview"], b["from"])
            if parsed and parsed.get("amount"):
                new_bills.append(parsed)
                # Log to Firefly
                try:
                    payee = parsed["payee"]
                    # Map known payees to Firefly categories
                    cat_map = {
                        "hydro": "Electricity (Hydro One)",
                        "enbridge": "Gas & Heating",
                        "union gas": "Gas & Heating",
                        "rogers": "Internet",
                        "insurance": "Insurance - Home",
                    }
                    category = "Uncategorized"
                    for key, cat in cat_map.items():
                        if key in payee.lower():
                            category = cat
                            break
                    firefly.log_transaction(
                        description=payee,
                        amount=parsed["amount"],
                        category=category,
                        destination_name=payee,
                    )
                except Exception as e:
                    log.warning(f"Firefly bill log failed: {e}")

        if new_bills:
            lines = ["BILL ALERT\n"]
            for b in new_bills:
                lines.append(f"  {b['payee']}: ${b['amount']:.2f}")
                if b.get("due_date"):
                    lines.append(f"  Due: {b['due_date']}")
                lines.append("")
            await app.bot.send_message(chat_id=owner_id, text="\n".join(lines))
            log.info(f"Bill scan: {len(new_bills)} bills found and logged")
    except Exception as e:
        log.error(f"Bill scan error: {e}")


async def job_payment_reminders(app: Application):
    """10:00 AM - Check payment trackers and DM owner."""
    owner_id = config.FAMILY_MEMBERS[config.OWNER]["telegram_id"]
    owner_nick = config.FAMILY_MEMBERS[config.OWNER]["nickname"]
    for tracker in config.WORKSPACE.glob("*_payment_tracker.json"):
        try:
            with open(tracker) as f:
                data = json.load(f)
            if data.get("status") == "pending":
                msg = (
                    f"Hey {owner_nick}, friendly reminder:\n"
                    f"You owe {data['creditor']} ${data['amount']:.2f}\n"
                    f"For: {data.get('purpose', 'unknown')}\n\n"
                    f"Let me know when it's paid and I'll stop bugging you."
                )
                await app.bot.send_message(chat_id=owner_id, text=msg)
                log.info(f"Payment reminder sent: {tracker.name}")
        except (json.JSONDecodeError, KeyError) as e:
            log.warning(f"Bad payment tracker {tracker.name}: {e}")


async def job_check_reminders(app: Application):
    """Every minute - check for due reminders and deliver them."""
    # Deliver Matrix reminders first (IDs starting with @)
    try:
        await matrix_client.deliver_matrix_reminders()
    except Exception as e:
        log.warning(f"Matrix reminder check failed: {e}")

    # Deliver Telegram reminders (numeric IDs)
    due = reminders.get_due_reminders()
    for r in due:
        target = r["telegram_id"]
        if target.startswith("@") and ":" in target:
            continue  # Already handled by Matrix delivery above
        try:
            msg = f"Hey {r['user_name'].title()}! Reminder:\n\n{r['message']}"
            await app.bot.send_message(chat_id=target, text=msg)
            reminders.mark_delivered(r["id"])
            log.info(f"Reminder delivered to {r['user_name']}: {r['message']}")
        except Exception as e:
            log.error(f"Reminder delivery failed: {e}")


async def job_daily_log(app: Application):
    """11:00 PM - Write daily conversation log to disk."""
    conversation_log.write_daily_log()


# ─── Main ───

async def async_main():
    token = config.TELEGRAM_TOKEN_FILE.read_text().strip()

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("bought", cmd_bought))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("spent", cmd_spent))
    app.add_handler(CommandHandler("owe", cmd_owe))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("reminders", cmd_my_reminders))
    app.add_handler(CommandHandler("cancel", cmd_cancel_reminder))
    app.add_handler(CommandHandler("recipes", cmd_recipes))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(CommandHandler("inbox", cmd_inbox))
    app.add_handler(CommandHandler("bills", cmd_bills))
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    # Photos (receipt parsing)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Free-form text (AI fallback) - must be last
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Initialize and start Telegram (non-blocking)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram polling started")

    # Start scheduler
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    scheduler.add_job(
        job_morning_briefing, CronTrigger(hour=6, minute=30),
        args=[app], id="morning_briefing",
    )
    scheduler.add_job(
        job_grocery_push, CronTrigger(hour=9, minute=0),
        args=[app], id="grocery_push",
    )
    scheduler.add_job(
        job_low_stock_alert, CronTrigger(hour=18, minute=0),
        args=[app], id="low_stock_alert",
    )
    scheduler.add_job(
        job_bill_scan, CronTrigger(hour=8, minute=0),
        args=[app], id="bill_scan",
    )
    scheduler.add_job(
        job_payment_reminders, CronTrigger(hour=10, minute=0),
        args=[app], id="payment_reminders",
    )
    scheduler.add_job(
        job_check_reminders, IntervalTrigger(minutes=1),
        args=[app], id="check_reminders",
    )
    scheduler.add_job(
        job_daily_log, CronTrigger(hour=23, minute=0),
        args=[app], id="daily_log",
    )
    scheduler.start()
    log.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    # Start voice API
    await voice_api.start_voice_api()

    # Start Matrix client (runs its own sync loop)
    async def _run_matrix():
        try:
            await matrix_client.start()
        except Exception as e:
            log.error(f"Matrix client crashed: {e}", exc_info=True)

    matrix_task = asyncio.create_task(_run_matrix())
    log.info("Matrix client starting")

    # Run forever until interrupted
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        conversation_log.flush_buffer()
        log.info("Conversation log flushed on shutdown")
        await matrix_client.stop()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main():
    log.info("Family Bot starting...")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
