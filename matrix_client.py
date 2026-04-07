"""Matrix client for Frank - runs alongside Telegram bot.
Uses matrix-nio to connect to Synapse homeserver."""
import asyncio
import io
import logging
import os
import random
import re
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from nio import (
    AsyncClient,
    AsyncClientConfig,
    MatrixRoom,
    RoomMessageText,
    RoomMessageImage,
    RoomMessageFile,
    RoomEncryptedFile,
    RoomEncryptedImage,
    DownloadResponse,
    DownloadError,
    LoginResponse,
    SyncResponse,
    Event,
    MegolmEvent,
    KeyVerificationStart,
    KeyVerificationCancel,
    KeyVerificationKey,
    KeyVerificationMac,
    ToDeviceError,
    RoomSendResponse,
)
from nio.store import SqliteStore

import config
import ai
import humanize
import episodes
import coordinator
import ultraplan
import style_learner
import briefing
import conversation_log
import reminders

# Optional modules — only import if enabled
db = None
buddy = None
recipes = None
firefly = None
email_client = None
agentmail_client = None
mem0_memory = None
gmail_client = None
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
if config.GMAIL_ENABLED:
    import gmail_client
if config.AGENTMAIL_ENABLED:
    import agentmail_client
if config.MEM0_ENABLED:
    import mem0_memory
if config.IMMICH_ENABLED:
    import immich_client

log = logging.getLogger("family-bot.matrix")
log.setLevel(logging.DEBUG)

client: AsyncClient = None
_first_sync_done = False
_batcher = humanize.MessageBatcher(delay=2.5)
_pending_receipts = {}  # room_id -> receipt/statement data awaiting confirmation
_pending_gmail_auth = set()  # sender matrix IDs awaiting Gmail auth code
_pending_email_setup = {}  # sender -> setup flow state dict
_recent_file_rooms = {}  # room_id -> timestamp, tracks rooms with recent file uploads


def _trust_all_devices():
    """Trust all known devices for all users so we can send encrypted messages."""
    if not client or not client.olm:
        return
    try:
        for user_id in list(client.device_store.users):
            devices = client.device_store.active_user_devices(user_id)
            # Handle both dict and generator returns
            if hasattr(devices, 'values'):
                device_list = devices.values()
            elif hasattr(devices, '__iter__'):
                device_list = list(devices)
            else:
                continue
            for device in device_list:
                if hasattr(device, 'device_id'):
                    if not client.olm.is_device_verified(device):
                        client.verify_device(device)
                        log.info(f"Trusted device {device.device_id} for {user_id}")
    except Exception as e:
        log.warning(f"Device trust error: {e}", exc_info=True)


def _matrix_user_to_name(user_id: str) -> str:
    """Convert @user:homeserver -> username via config lookup."""
    return config.MATRIX_ID_TO_NAME.get(user_id, user_id.split(":")[0].lstrip("@"))


def _is_private(room: MatrixRoom) -> bool:
    """Check if this is a DM (2 members) vs group room."""
    return room.member_count <= 2


async def _send(room_id: str, text: str, html: str = None):
    """Send a message to a Matrix room."""
    content = {
        "msgtype": "m.text",
        "body": text,
    }
    if html:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = html
    await client.room_send(room_id, "m.room.message", content)


async def _send_to_user(matrix_id: str, text: str):
    """Send a DM to a user by their Matrix ID."""
    # Find or create a DM room with this user
    for room_id, room in client.rooms.items():
        if room.member_count == 2 and matrix_id in [m.user_id for m in room.users.values()]:
            await _send(room_id, text)
            return
    # No existing DM room, create one
    resp = await client.room_create(
        invite=[matrix_id],
        is_direct=True,
    )
    if hasattr(resp, "room_id"):
        await _send(resp.room_id, text)


async def _send_image(room_id: str, file_path: str, body: str = "image"):
    """Send an image file to a Matrix room."""
    import mimetypes
    mime = mimetypes.guess_type(file_path)[0] or "image/jpeg"
    with open(file_path, "rb") as f:
        data = f.read()
    resp, _ = await client.upload(data, content_type=mime, filename=body)
    if hasattr(resp, "content_uri"):
        content = {
            "msgtype": "m.image",
            "body": body,
            "url": resp.content_uri,
            "info": {"mimetype": mime, "size": len(data)},
        }
        await client.room_send(room_id, "m.room.message", content)


# ─── Command Handlers ───

async def cmd_list(room_id: str):
    items = db.get_shopping_list()
    if not items:
        await _send(room_id, "Shopping list is empty! Nothing to buy.")
        return

    cat_emoji = {
        "PRODUCE": "🥬", "DAIRY": "🥛", "MEAT": "🥩", "BAKERY": "🍞",
        "PANTRY": "🥫", "FROZEN": "🧊", "HOUSEHOLD": "🧹", "PET": "🐕",
        "OTHER": "📦",
    }

    by_cat = {}
    for item in items:
        cat = (item["category"] or "other").upper()
        qty = f"  x{item['qty']}" if item["qty"] else ""
        by_cat.setdefault(cat, []).append(f"  {item['name']}{qty}")

    lines = ["SHOPPING LIST\n"]
    for cat, entries in sorted(by_cat.items()):
        emoji = cat_emoji.get(cat, "📦")
        lines.append(f"{emoji} {cat}")
        lines.extend(entries)
        lines.append("")

    lines.append(f"{len(items)} items")

    # Add dinner plan ingredients section
    try:
        plans = db.get_meal_plan_ingredients(upcoming_only=True)
        for p in plans:
            if p["ingredients"]:
                lines.append(f"\n🍽️ FOR {p['date']} — {p['meal'].upper()}")
                for ing in p["ingredients"]:
                    lines.append(f"  {ing}")
    except Exception:
        pass

    msg = "\n".join(lines)
    await _send(room_id, msg)
    ai.inject_context(f"matrix_{room_id}", "checked shopping list", msg[:300])


async def cmd_dinner(room_id: str):
    """Show upcoming dinner plans."""
    if not db:
        return
    plans = db.get_meal_plan_ingredients(upcoming_only=True)
    if not plans:
        await _send(room_id, "No dinner plans right now. Tell me what you're making and when!")
        return
    lines = ["DINNER PLANS\n"]
    for p in plans:
        lines.append(f"🍽️ {p['date']} — {p['meal']}")
        if p["ingredients"]:
            for ing in p["ingredients"]:
                lines.append(f"  - {ing}")
        lines.append("")
    await _send(room_id, "\n".join(lines))


async def cmd_add(room_id: str, args: str, user_name: str):
    if not args:
        await _send(room_id, "Usage: !add <item name>")
        return
    added, existing = db.add_shopping_item(args, requested_by=user_name)
    if added:
        await _send(room_id, f"Added {args} to the list!")
    else:
        await _send(room_id, f"'{existing}' is already on the list.")


async def cmd_bought(room_id: str, args: str, user_name: str):
    if not args:
        await _send(room_id, "Usage: !bought <item name>")
        return
    if db.mark_item_bought(args, bought_by=user_name):
        db.record_event(args, "bought", note=f"Bought by {user_name}")
        await _send(room_id, f"Marked {args} as bought!")
    else:
        await _send(room_id, f"Couldn't find '{args}' on the shopping list.")


async def cmd_stock(room_id: str):
    items = db.get_inventory()
    if not items:
        await _send(room_id, "No inventory data.")
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

    await _send(room_id, "\n".join(lines))


async def cmd_spent(room_id: str, args: str, user_name: str):
    parts = args.split(None, 1) if args else []
    if len(parts) < 2:
        await _send(room_id, "Usage: !spent <amount> <store>\nExample: !spent 45.50 Fortinos")
        return
    try:
        amount = float(parts[0].replace("$", ""))
    except ValueError:
        await _send(room_id, "Amount must be a number. Example: !spent 45.50 Fortinos")
        return

    store = parts[1]
    db.log_spend(store, amount)
    try:
        import finance
        finance.log_receipt(user_name, store, amount)
    except Exception as e:
        log.warning(f"Finance log failed: {e}")
    if firefly:
        try:
            firefly.log_receipt(store, amount)
        except Exception as e:
            log.warning(f"Firefly log failed: {e}")
    await _send(room_id, f"Logged ${amount:.2f} at {store}")


async def cmd_owe(room_id: str):
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
        await _send(room_id, "OUTSTANDING PAYMENTS:\n\n" + "\n".join(lines))
    else:
        await _send(room_id, "No outstanding payments!")


async def cmd_summary(room_id: str):
    if not firefly:
        await _send(room_id, "Finance tracking is not configured.")
        return
    try:
        data = firefly.get_monthly_summary()
        lines = [f"SPENDING THIS MONTH: ${data['total']:.2f}\n"]
        for cat, amt in sorted(data["by_category"].items(), key=lambda x: -x[1]):
            lines.append(f"  {cat}: ${amt:.2f}")
        await _send(room_id, "\n".join(lines))
    except Exception as e:
        log.error(f"Firefly summary error: {e}")
        await _send(room_id, "Couldn't fetch spending data right now.")


async def cmd_balance(room_id: str, room: MatrixRoom, sender: str):
    if not firefly:
        await _send(room_id, "Finance tracking is not configured.")
        return
    if not _is_private(room):
        await _send(room_id, "I'll DM you the balances -- that's private info.")
        try:
            balances = firefly.get_account_balances()
            lines = ["ACCOUNT BALANCES\n"]
            for b in balances:
                lines.append(f"  {b['name']}: ${float(b['balance']):,.2f} {b['currency']}")
            await _send_to_user(sender, "\n".join(lines))
        except Exception as e:
            log.error(f"Firefly balance error: {e}")
            await _send_to_user(sender, "Couldn't fetch balances right now.")
        return
    try:
        balances = firefly.get_account_balances()
        lines = ["ACCOUNT BALANCES\n"]
        for b in balances:
            lines.append(f"  {b['name']}: ${float(b['balance']):,.2f} {b['currency']}")
        await _send(room_id, "\n".join(lines))
    except Exception as e:
        log.error(f"Firefly balance error: {e}")
        await _send(room_id, "Couldn't fetch balances right now.")


async def cmd_inbox(room_id: str, room: MatrixRoom, sender: str):
    user_name = _matrix_user_to_name(sender)
    member = config.FAMILY_MEMBERS.get(user_name, {})
    has_email = member.get("email")

    if not has_email and user_name != config.OWNER:
        await _send(room_id, "You don't have email set up yet. Say 'set up my email' to get started!")
        return
    if not has_email and not email_client:
        await _send(room_id, "Email is not configured.")
        return

    is_dm = _is_private(room)
    if not is_dm:
        await _send(room_id, "I'll DM you — email is private.")

    async def send_msg(msg):
        if is_dm:
            await _send(room_id, msg)
        else:
            await _send_to_user(sender, msg)

    try:
        nickname = member.get("nickname", user_name.title())
        em_cfg = member.get("email", {})
        em_type = em_cfg.get("type", "imap")

        # Get this user's emails
        if em_type == "gmail" and gmail_client:
            emails = gmail_client.get_unread(limit=5, member_name=user_name)
        elif email_client:
            emails = email_client.get_unread(limit=5, member_name=user_name)
        else:
            await send_msg("Couldn't connect to your email.")
            return

        lines = [f"{nickname.upper()}'S EMAIL\n"]
        if emails:
            lines.append(f"{len(emails)} recent:\n")
            for e in emails:
                lines.append(f"From: {e['from']}")
                lines.append(f"Subject: {e.get('subject', '(no subject)')}")
                lines.append(f"Date: {e.get('date', '')}")
                lines.append("")
        else:
            lines.append("No unread emails.\n")

        # Only show bot's inbox to the owner
        if user_name == config.OWNER and agentmail_client:
            frank_mail = agentmail_client.get_unread(limit=5)
            lines.append(f"{config.BOT_NAME.upper()}'S EMAIL ({config.AGENTMAIL_ADDRESS})\n")
            if frank_mail:
                lines.append(f"{len(frank_mail)} recent:\n")
                for e in frank_mail:
                    lines.append(f"From: {e['from']}")
                    lines.append(f"Subject: {e['subject']}")
                    lines.append(f"Date: {e['date'][:16]}")
                    lines.append("")
            else:
                lines.append("No emails.")

        msg = "\n".join(lines)
        await send_msg(msg)
        ai.inject_context(f"matrix_{room_id}", "checked email inbox", msg[:500])

    except Exception as e:
        log.error(f"Email error for {user_name}: {e}")
        await send_msg("Couldn't check email right now.")


async def cmd_bills(room_id: str, room: MatrixRoom, sender: str):
    user_name = _matrix_user_to_name(sender)
    member = config.FAMILY_MEMBERS.get(user_name, {})
    has_email = member.get("email")

    if not has_email and user_name != config.OWNER:
        await _send(room_id, "You don't have email set up yet. Say 'set up my email' to get started!")
        return
    if not has_email and not email_client:
        await _send(room_id, "Email is not configured.")
        return

    is_dm = _is_private(room)
    if not is_dm:
        await _send(room_id, "I'll DM you the bills.")

    async def send_msg(msg):
        if is_dm:
            await _send(room_id, msg)
        else:
            await _send_to_user(sender, msg)

    try:
        em_cfg = member.get("email", {})
        em_type = em_cfg.get("type", "imap")

        if em_type == "gmail" and gmail_client:
            bills = gmail_client.get_bills(limit=5, member_name=user_name)
            # Normalize gmail format
            bills = [{"from": b.get("from", ""), "subject": b.get("subject", ""),
                       "date": b.get("date", ""), "body_preview": b.get("snippet", "")} for b in bills]
        elif email_client:
            bills = email_client.get_bills(limit=5, member_name=user_name)
        else:
            await send_msg("Couldn't connect to your email.")
            return

        if not bills:
            await send_msg("No bills found.")
            return

        lines = ["RECENT BILLS\n"]
        for b in bills:
            lines.append(f"From: {b['from']}")
            lines.append(f"Subject: {b['subject']}")
            lines.append(f"Date: {b['date']}")
            parsed = email_client.parse_bill_email(b["subject"], b.get("body_preview", ""), b["from"])
            if parsed and parsed.get("amount"):
                lines.append(f"Amount: ${parsed['amount']:.2f}")
                if parsed.get("due_date"):
                    lines.append(f"Due: {parsed['due_date']}")
            lines.append("")

        await send_msg("\n".join(lines))
    except Exception as e:
        log.error(f"Bills error for {user_name}: {e}")
        await send_msg("Couldn't check bills right now.")


async def cmd_remind(room_id: str, args: str, sender: str, user_name: str):
    if not args:
        await _send(room_id,
            "Usage: !remind <when> <what>\n"
            "Examples:\n"
            "  !remind in 30 minutes check the oven\n"
            "  !remind tomorrow call the dentist\n"
            "  !remind at 3pm pick up the kids"
        )
        return

    remind_at, message = reminders.parse_reminder_time(args)
    if not remind_at:
        await _send(room_id, "I couldn't figure out the time. Try: !remind in 30 minutes check the oven")
        return

    message = message.strip()
    for prefix in ("to ", "that ", "me to ", "me that ", "remind me to ", "remind me "):
        if message.lower().startswith(prefix):
            message = message[len(prefix):]
            break

    if not message:
        message = args

    # Store Matrix user ID for delivery
    reminders.add_reminder(user_name, sender, message, remind_at)
    await _send(room_id,
        f"Got it! I'll remind you: \"{message}\"\n"
        f"When: {remind_at.strftime('%B %d at %I:%M %p')}"
    )


async def cmd_my_reminders(room_id: str, user_name: str):
    pending = reminders.get_pending_for_user(user_name)
    if not pending:
        await _send(room_id, "No pending reminders!")
        return

    lines = ["YOUR REMINDERS\n"]
    for r in pending:
        lines.append(f"  #{r['id']} - {r['message']}")
        lines.append(f"    When: {r['remind_at']}")
        lines.append("")
    lines.append("Cancel with !cancel <number>")
    await _send(room_id, "\n".join(lines))


async def cmd_cancel_reminder(room_id: str, args: str):
    if not args:
        await _send(room_id, "Usage: !cancel <reminder number>")
        return
    try:
        rid = int(args.replace("#", ""))
    except ValueError:
        await _send(room_id, "Give me the reminder number, e.g. !cancel 3")
        return

    if reminders.cancel_reminder(rid):
        await _send(room_id, f"Reminder #{rid} cancelled.")
    else:
        await _send(room_id, f"Couldn't find reminder #{rid}.")


async def cmd_recipes(room_id: str, args: str):
    if args:
        results = recipes.search_recipes(args)
        if not results:
            await _send(room_id, f"No recipes found for '{args}'.")
            return
        lines = [f"RECIPES matching '{args}':\n"]
        for r in results:
            time_str = ""
            total = (r["prep_time"] or 0) + (r["cook_time"] or 0)
            if total:
                time_str = f" ({total} min)"
            lines.append(f"  #{r['id']} {r['name']}{time_str}")
        lines.append("\nUse !recipe <number> to see the full recipe.")
        await _send(room_id, "\n".join(lines))
    else:
        all_recipes = recipes.list_recipes()
        if not all_recipes:
            await _send(room_id, "No recipes saved yet.")
            return
        lines = ["ALL RECIPES:\n"]
        for r in all_recipes:
            time_str = ""
            total = (r["prep_time"] or 0) + (r["cook_time"] or 0)
            if total:
                time_str = f" ({total} min)"
            lines.append(f"  #{r['id']} {r['name']}{time_str}")
        lines.append("\nUse !recipe <number> for details, or !recipes <search> to search.")
        await _send(room_id, "\n".join(lines))


async def cmd_recipe(room_id: str, args: str):
    if not args:
        await _send(room_id, "Usage: !recipe <number>")
        return
    try:
        rid = int(args.replace("#", ""))
    except ValueError:
        await _send(room_id, "Give me the recipe number, e.g. !recipe 1")
        return

    data = recipes.get_recipe(rid)
    if not data:
        await _send(room_id, f"Recipe #{rid} not found.")
        return

    formatted = recipes.format_recipe(data)
    await _send(room_id, formatted)


async def cmd_summary(room_id: str, args: str, room: MatrixRoom, sender: str):
    """Monthly spending summary for the requesting user."""
    user_name = _matrix_user_to_name(sender)
    try:
        import finance
        summary = finance.get_monthly_summary(user_name)
        nickname = config.FAMILY_MEMBERS.get(user_name, {}).get("nickname", user_name.title())
        month_name = datetime.now().strftime("%B %Y")

        lines = [f"{nickname.upper()}'S FINANCES — {month_name}\n"]
        lines.append(f"Total spent: ${summary['total_spent']:.2f}")
        if summary['total_income'] > 0:
            lines.append(f"Total income: ${summary['total_income']:.2f}")
        lines.append(f"Transactions: {summary['transaction_count']}\n")

        if summary['by_category']:
            lines.append("By category:")
            for cat, total in summary['by_category'].items():
                lines.append(f"  {cat}: ${total:.2f}")

        is_dm = _is_private(room)
        if is_dm:
            await _send(room_id, "\n".join(lines))
        else:
            await _send(room_id, "I'll DM you — finances are private.")
            await _send_to_user(sender, "\n".join(lines))
    except Exception as e:
        log.error(f"Summary error for {user_name}: {e}")
        await _send(room_id, "Couldn't pull your summary right now.")


async def cmd_transactions(room_id: str, args: str, room: MatrixRoom, sender: str):
    """Show recent transactions for the requesting user."""
    user_name = _matrix_user_to_name(sender)
    try:
        import finance
        if args:
            txns = finance.search_transactions(user_name, args, limit=10)
            header = f"TRANSACTIONS MATCHING '{args}'"
        else:
            txns = finance.get_recent(user_name, limit=10)
            header = "RECENT TRANSACTIONS"

        if not txns:
            await _send(room_id, "No transactions found.")
            return

        lines = [f"{header}\n"]
        for tx in txns:
            sign = "+" if tx["tx_type"] == "deposit" else "-"
            lines.append(f"  {tx['date']}  {sign}${tx['amount']:.2f}  {tx['description']}  [{tx['category']}]")

        is_dm = _is_private(room)
        if is_dm:
            await _send(room_id, "\n".join(lines))
        else:
            await _send(room_id, "I'll DM you — finances are private.")
            await _send_to_user(sender, "\n".join(lines))
    except Exception as e:
        log.error(f"Transactions error for {user_name}: {e}")
        await _send(room_id, "Couldn't pull transactions right now.")


async def cmd_briefing(room_id: str):
    msg = briefing.build_briefing()
    await _send(room_id, msg)


async def cmd_help(room_id: str):
    sections = [f"HEY! I'm {config.BOT_NAME}. Here's what I can do:\n"]

    if config.GROCERY_ENABLED:
        sections.append(
            "GROCERIES\n"
            "!list - Shopping list\n"
            "!add <item> - Add to list\n"
            "!bought <item> - Mark as bought\n"
            "!stock - Full inventory\n"
            "!spent <amount> <store> - Log a purchase\n"
            "!owe - Who owes who\n"
            "!dinner - View planned dinners"
        )

    if config.FINANCE_ENABLED or config.FIREFLY_ENABLED:
        sections.append(
            "MONEY (DM only)\n"
            "!summary - Monthly spending breakdown\n"
            "!transactions - Recent transactions\n"
            "!transactions <search> - Search transactions"
        )

    sections.append(
        "REMINDERS\n"
        "!remind <when> <what> - Set a reminder\n"
        "!reminders - My pending reminders\n"
        "!cancel <#> - Cancel a reminder"
    )

    if config.RECIPES_ENABLED:
        sections.append(
            "RECIPES\n"
            "!recipes - List all recipes\n"
            "!recipes <search> - Search recipes\n"
            "!recipe <#> - Show full recipe"
        )

    email_help = "EMAIL\n!setup - Connect your Gmail for daily email scanning"
    if config.EMAIL_ENABLED or config.GMAIL_ENABLED:
        email_help += "\n!inbox - Check emails (DM only)\n!bills - Recent bills (DM only)"
        if config.AGENTMAIL_ENABLED:
            email_help += "\n!send <to> <subject> | <body> - Send email"
    sections.append(email_help)

    if config.IMMICH_ENABLED:
        sections.append(
            "PHOTOS\n"
            "!photos <search> - Search family photos\n"
            "!albums - List photo albums\n"
            "!people - Recognized people in photos"
        )

    if config.BUDDY_ENABLED:
        sections.append(
            "BUDDY\n"
            "!buddy - Check your companion pet\n"
            "!buddy <name> - Name your buddy"
        )

    sections.append(
        "OTHER\n"
        "!briefing - Morning briefing\n"
        "!help - This message\n\n"
        "Or just talk to me naturally."
    )

    await _send(room_id, "\n\n".join(sections))


# ─── Message Router ───

async def cmd_send_email(room_id: str, args: str, room: MatrixRoom, sender: str):
    """Send an email. Usage: !send <to> <subject> | <body>"""
    if not agentmail_client:
        await _send(room_id, "Email sending is not configured.")
        return
    user_name = _matrix_user_to_name(sender)
    if user_name != config.OWNER:
        await _send(room_id, "Email sending is owner-only.")
        return
    if not _is_private(room):
        await _send(room_id, "Use this in a DM for privacy.")
        return
    if not args or "|" not in args:
        await _send(room_id, "Usage: !send <to> <subject> | <body>\nExample: !send john@example.com Quick question | Hey John, are we still on for Friday?")
        return

    header, body = args.split("|", 1)
    parts = header.strip().split(None, 1)
    if len(parts) < 2:
        await _send(room_id, "Need both email address and subject. Example: !send john@example.com Quick question | message body")
        return

    to_addr = parts[0]
    subject = parts[1].strip()
    body = body.strip()

    try:
        agentmail_client.send_email(to_addr, subject, body)
        await _send(room_id, f"Sent to {to_addr}: {subject}")
    except Exception as e:
        log.error(f"Email send error: {e}")
        await _send(room_id, f"Failed to send: {e}")


async def cmd_myprofile(room_id: str, user_name: str):
    info = style_learner.format_profile(user_name)
    await _send(room_id, info)


async def cmd_resetprofile(room_id: str, user_name: str):
    result = style_learner.reset_profile(user_name)
    await _send(room_id, result)


async def cmd_buddy(room_id: str, args: str, user_name: str):
    if args:
        # Name the buddy
        result = buddy.name_buddy(user_name, args)
        await _send(room_id, result)
    else:
        info = buddy.format_buddy(user_name)
        await _send(room_id, info)


async def cmd_photos(room_id: str, args: str):
    """Search photos. Usage: !photos <query>"""
    if not immich_client:
        await _send(room_id, "Photo library is not configured.")
        return
    if not args:
        await _send(room_id, "Usage: !photos <search>\nExamples: !photos beach sunset, !photos birthday cake")
        return
    results = immich_client.search_photos(args, limit=5)
    await _send(room_id, immich_client.format_results(results, args))
    if results:
        # Send first result as thumbnail
        path = immich_client.download_thumbnail(results[0]["id"])
        if path:
            await _send_image(room_id, path, f"Top result for '{args}'")


async def cmd_albums(room_id: str):
    """List photo albums."""
    if not immich_client:
        await _send(room_id, "Photo library is not configured.")
        return
    albums = immich_client.get_albums()
    if not albums:
        await _send(room_id, "No albums found.")
        return
    lines = ["PHOTO ALBUMS\n"]
    for a in albums[:15]:
        lines.append(f"  {a['name']} ({a['count']} photos)")
    await _send(room_id, "\n".join(lines))


async def cmd_people(room_id: str):
    """List recognized people in photos."""
    if not immich_client:
        await _send(room_id, "Photo library is not configured.")
        return
    people = immich_client.get_people()
    if not people:
        await _send(room_id, "No recognized people yet.")
        return
    lines = ["PEOPLE IN PHOTOS\n"]
    for p in people[:20]:
        lines.append(f"  {p['name']}")
    lines.append("\nUse !photos <name> to see their photos.")
    await _send(room_id, "\n".join(lines))


async def cmd_setup_email(room_id: str, args: str, sender: str, user_name: str):
    """Walk a user through email setup via chat."""
    # Check if already set up
    member = config.FAMILY_MEMBERS.get(user_name, {})
    if member.get("email"):
        await _send(room_id, "Your email is already connected! I scan it daily at 8 AM. Ask me to check your email anytime.")
        return

    # Start the setup flow
    _pending_email_setup[sender] = {"step": "choose_type", "user_name": user_name}
    await _send(room_id,
        "Let's get your email connected! What kind of email do you have?\n\n"
        "1. **Gmail** — easiest, just need an app password\n"
        "2. **Other** (Outlook, Yahoo, ProtonMail, etc.) — need IMAP server details\n\n"
        "Reply with **1** or **2**."
    )


async def _handle_email_setup_flow(room_id: str, text: str, sender: str):
    """Handle multi-step email setup conversation."""
    setup = _pending_email_setup.get(sender)
    if not setup:
        return False

    step = setup["step"]
    user_name = setup["user_name"]
    lower = text.strip().lower()

    if step == "choose_type":
        if lower in ("1", "gmail", "google"):
            setup["type"] = "gmail_imap"
            setup["imap_host"] = "imap.gmail.com"
            setup["imap_port"] = 993
            setup["smtp_host"] = "smtp.gmail.com"
            setup["smtp_port"] = 587
            setup["step"] = "gmail_email"
            await _send(room_id, "Great! What's your Gmail address?")
        elif lower in ("2", "other", "outlook", "yahoo", "protonmail"):
            setup["type"] = "imap"
            setup["step"] = "other_email"
            await _send(room_id, "What's your email address?")
        else:
            await _send(room_id, "Just reply **1** for Gmail or **2** for other.")
        return True

    elif step == "gmail_email":
        if "@" in text:
            setup["user"] = text.strip()
            setup["step"] = "gmail_password"
            await _send(room_id,
                f"Got it — {text.strip()}\n\n"
                "Now I need a Gmail **App Password**. Here's how to get one:\n\n"
                "1. Go to myaccount.google.com/apppasswords on your phone or laptop\n"
                "2. You may need to enable 2-Step Verification first (myaccount.google.com/signinoptions/two-step-verification)\n"
                "3. Create an app password — name it 'Frank' or anything you like\n"
                "4. Google will show you a 16-character password — paste it here\n\n"
                "It'll look something like: abcd efgh ijkl mnop"
            )
        else:
            await _send(room_id, "That doesn't look like an email address. Try again?")
        return True

    elif step == "gmail_password":
        # App passwords are 16 chars (with or without spaces)
        password = text.strip().replace(" ", "")
        if len(password) >= 12:
            setup["password"] = password
            setup["step"] = "confirm"
            await _send(room_id,
                f"Ready to connect:\n"
                f"- Email: {setup['user']}\n"
                f"- Server: Gmail (IMAP)\n\n"
                f"Want me to test the connection? Reply **yes** to confirm."
            )
        else:
            await _send(room_id, "That seems too short for an app password. It should be 16 characters (spaces are fine). Try again?")
        return True

    elif step == "other_email":
        if "@" in text:
            setup["user"] = text.strip()
            setup["step"] = "other_host"
            await _send(room_id,
                f"Got it — {text.strip()}\n\n"
                "What's the IMAP server address? (e.g., imap.outlook.com, imap.mail.yahoo.com)"
            )
        else:
            await _send(room_id, "That doesn't look like an email address. Try again?")
        return True

    elif step == "other_host":
        setup["imap_host"] = text.strip()
        setup["imap_port"] = 993
        setup["smtp_host"] = text.strip().replace("imap.", "smtp.")
        setup["smtp_port"] = 587
        setup["step"] = "other_password"
        await _send(room_id, "And what's your password (or app password)?")
        return True

    elif step == "other_password":
        setup["password"] = text.strip()
        setup["step"] = "confirm"
        await _send(room_id,
            f"Ready to connect:\n"
            f"- Email: {setup['user']}\n"
            f"- Server: {setup['imap_host']}\n\n"
            f"Reply **yes** to test the connection."
        )
        return True

    elif step == "confirm":
        if lower in ("yes", "y", "yep", "yeah", "sure", "go ahead", "do it"):
            # Test the connection
            try:
                import imaplib
                import ssl
                ctx = ssl.create_default_context()
                mail = imaplib.IMAP4_SSL(setup["imap_host"], setup["imap_port"], ssl_context=ctx)
                mail.login(setup["user"], setup["password"])
                mail.logout()
            except Exception as e:
                log.warning(f"Email test failed for {user_name}: {e}")
                await _send(room_id,
                    f"Connection failed: {e}\n\n"
                    "Double-check your password/app password and try again. "
                    "Say 'set up my email' to start over."
                )
                _pending_email_setup.pop(sender, None)
                return True

            # Save the account
            _save_email_account(user_name, {
                "type": "imap",
                "imap_host": setup["imap_host"],
                "imap_port": setup["imap_port"],
                "smtp_host": setup.get("smtp_host", ""),
                "smtp_port": setup.get("smtp_port", 587),
                "user": setup["user"],
                "password": setup["password"],
            })

            _pending_email_setup.pop(sender, None)
            await _send(room_id,
                "Connected! I'll scan your email every day at 8 AM and let you know about any bills or important messages. "
                "You can also ask me to check your email anytime."
            )
            log.info(f"Email setup complete for {user_name}: {setup['user']}")
        elif lower in ("no", "n", "cancel", "nope", "nevermind"):
            _pending_email_setup.pop(sender, None)
            await _send(room_id, "No problem, cancelled. Say 'set up my email' anytime to try again.")
        else:
            await _send(room_id, "Reply **yes** to test and save, or **no** to cancel.")
        return True

    return False


def _save_email_account(user_name, account_data):
    """Save a user's email config to email_accounts.json and update runtime config."""
    import json
    accounts_file = config._CONFIG_DIR / "email_accounts.json"
    accounts = {}
    if accounts_file.exists():
        with open(accounts_file) as f:
            accounts = json.load(f)

    accounts[user_name] = account_data
    with open(accounts_file, "w") as f:
        json.dump(accounts, f, indent=2)
    os.chmod(accounts_file, 0o600)

    # Update runtime config
    config.FAMILY_MEMBERS[user_name]["email"] = account_data
    log.info(f"Email account saved for {user_name}")


# Build command registry — only register enabled features
COMMANDS = {
    # Always available
    "remind": lambda rid, args, room, sender, uname: cmd_remind(rid, args, sender, uname),
    "reminders": lambda rid, args, room, sender, uname: cmd_my_reminders(rid, uname),
    "cancel": lambda rid, args, room, sender, uname: cmd_cancel_reminder(rid, args),
    "briefing": lambda rid, args, room, sender, uname: cmd_briefing(rid),
    "myprofile": lambda rid, args, room, sender, uname: cmd_myprofile(rid, uname),
    "resetprofile": lambda rid, args, room, sender, uname: cmd_resetprofile(rid, uname),
    "help": lambda rid, args, room, sender, uname: cmd_help(rid),
    "setup": lambda rid, args, room, sender, uname: cmd_setup_email(rid, args, sender, uname),
}

# Grocery / inventory
if config.GROCERY_ENABLED:
    COMMANDS.update({
        "list": lambda rid, args, room, sender, uname: cmd_list(rid),
        "add": lambda rid, args, room, sender, uname: cmd_add(rid, args, uname),
        "bought": lambda rid, args, room, sender, uname: cmd_bought(rid, args, uname),
        "stock": lambda rid, args, room, sender, uname: cmd_stock(rid),
        "spent": lambda rid, args, room, sender, uname: cmd_spent(rid, args, uname),
        "owe": lambda rid, args, room, sender, uname: cmd_owe(rid),
        "dinner": lambda rid, args, room, sender, uname: cmd_dinner(rid),
    })

# Finance
if config.FINANCE_ENABLED:
    COMMANDS.update({
        "summary": lambda rid, args, room, sender, uname: cmd_summary(rid, args, room, sender),
        "transactions": lambda rid, args, room, sender, uname: cmd_transactions(rid, args, room, sender),
    })
elif config.FIREFLY_ENABLED:
    COMMANDS.update({
        "summary": lambda rid, args, room, sender, uname: cmd_summary(rid, args, room, sender),
    })

# Email
if config.EMAIL_ENABLED or config.GMAIL_ENABLED:
    COMMANDS.update({
        "inbox": lambda rid, args, room, sender, uname: cmd_inbox(rid, room, sender),
        "bills": lambda rid, args, room, sender, uname: cmd_bills(rid, room, sender),
    })
if config.AGENTMAIL_ENABLED:
    COMMANDS["send"] = lambda rid, args, room, sender, uname: cmd_send_email(rid, args, room, sender)

# Recipes
if config.RECIPES_ENABLED:
    COMMANDS.update({
        "recipes": lambda rid, args, room, sender, uname: cmd_recipes(rid, args),
        "recipe": lambda rid, args, room, sender, uname: cmd_recipe(rid, args),
    })

# Buddy
if config.BUDDY_ENABLED:
    COMMANDS["buddy"] = lambda rid, args, room, sender, uname: cmd_buddy(rid, args, uname)

# Photos
if config.IMMICH_ENABLED:
    COMMANDS.update({
        "photos": lambda rid, args, room, sender, uname: cmd_photos(rid, args),
        "albums": lambda rid, args, room, sender, uname: cmd_albums(rid),
        "people": lambda rid, args, room, sender, uname: cmd_people(rid),
    })


async def _download_matrix_file(mxc_url: str, filename: str, key=None, hashes=None, iv=None) -> str:
    """Download a file from Matrix and return the local path. Decrypts if key provided."""
    resp = await client.download(mxc_url)
    if isinstance(resp, DownloadError):
        raise RuntimeError(f"Download failed: {resp}")

    data = resp.body
    if key and hashes and iv:
        from nio.crypto import decrypt_attachment
        data = decrypt_attachment(data, key["k"], hashes["sha256"], iv)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}")
    tmp.write(data)
    tmp.close()
    return tmp.name


async def _handle_receipt_image(room_id: str, user_name: str, local_path: str):
    """Parse a receipt image and present results."""
    try:
        receipt_data = ai.parse_receipt_image(local_path)
        store = receipt_data.get("store", "Unknown")
        total = receipt_data.get("total", 0)
        items = receipt_data.get("items", [])

        summary = f"**Receipt from {store}** -- ${total:.2f}\n"
        for item in items[:15]:
            summary += f"  - {item.get('name', '?')}: ${item.get('price', 0):.2f}\n"
        if len(items) > 15:
            summary += f"  ...and {len(items) - 15} more items\n"
        summary += f"\nWant me to log this to Firefly? (say **yes** or **log it**)"
        await _send(room_id, summary)

        _pending_receipts[room_id] = {
            "store": store, "total": total, "items": items, "user": user_name,
        }
    except Exception as e:
        log.error(f"Receipt parse failed: {e}")
        await _send(room_id, f"I got the image but couldn't parse it as a receipt. Error: {e}")


async def _handle_pdf(room_id: str, user_name: str, local_path: str):
    """Parse a PDF (bank statement) and present results."""
    try:
        import firefly
        result = ai.parse_bank_statement(local_path)
        transactions = result.get("transactions", [])
        account = result.get("account", "Unknown")
        period = result.get("period", "")
        total_in = result.get("total_deposits", 0)
        total_out = result.get("total_withdrawals", 0)

        # Detect which Firefly account this maps to (check account name + filename + raw text)
        raw_text = result.get("_raw_text", "")
        acct_id, acct_type = firefly.detect_account(account, local_path, raw_text) if firefly else (None, "asset")
        acct_label = "credit card" if acct_type == "liability" else "bank account"

        summary = f"**{account}** -- {period}\n"
        summary += f"Detected as: **{acct_label}** (Firefly account #{acct_id})\n"
        summary += f"Deposits: ${total_in:.2f} | Withdrawals: ${total_out:.2f}\n\n"
        if transactions:
            summary += f"**{len(transactions)} transactions found:**\n"
            for tx in transactions[:20]:
                amt = tx.get('amount', 0)
                sign = "+" if tx.get('type') == 'deposit' else "-"
                summary += f"  {sign}${abs(amt):.2f} -- {tx.get('description', '?')} ({tx.get('date', '')})\n"
            if len(transactions) > 20:
                summary += f"  ...and {len(transactions) - 20} more\n"
        summary += f"\nWant me to log these to Firefly? (say **yes** or **log it**)"
        await _send(room_id, summary)

        _pending_receipts[room_id] = {
            "type": "bank_statement", "transactions": transactions,
            "account": account, "user": user_name,
            "account_id": acct_id, "account_type": acct_type,
        }
    except Exception as e:
        log.error(f"Bank statement parse failed: {e}")
        await _send(room_id, f"I got the PDF but couldn't parse it. Error: {e}")


async def on_encrypted_image(room: MatrixRoom, event: RoomEncryptedImage):
    """Handle encrypted image messages (receipts, photos)."""
    if event.sender == client.user_id or not _first_sync_done:
        return
    user_name = _matrix_user_to_name(event.sender)
    room_id = room.room_id
    filename = event.body or "image.jpg"
    log.info(f"Encrypted image from {user_name}: {filename}")
    import time as _time
    _recent_file_rooms[room_id] = _time.time()

    try:
        local_path = await _download_matrix_file(event.url, filename, event.key, event.hashes, event.iv)
        await _handle_receipt_image(room_id, user_name, local_path)
    except Exception as e:
        log.error(f"Failed to process encrypted image: {e}")
        await _send(room_id, f"Couldn't process that image: {e}")
    finally:
        if 'local_path' in locals() and os.path.exists(local_path):
            os.unlink(local_path)


async def on_encrypted_file(room: MatrixRoom, event: RoomEncryptedFile):
    """Handle encrypted file messages (PDFs)."""
    if event.sender == client.user_id or not _first_sync_done:
        return
    user_name = _matrix_user_to_name(event.sender)
    room_id = room.room_id
    filename = event.body or "file"

    if not filename.lower().endswith(".pdf"):
        log.debug(f"Ignoring non-PDF encrypted file: {filename}")
        return

    log.info(f"Encrypted PDF from {user_name}: {filename}")
    import time as _time
    _recent_file_rooms[room_id] = _time.time()
    await _send(room_id, f"Got **{filename}** -- processing...")

    try:
        local_path = await _download_matrix_file(event.url, filename, event.key, event.hashes, event.iv)
        await _handle_pdf(room_id, user_name, local_path)
    except Exception as e:
        log.error(f"Failed to process encrypted PDF: {e}")
        await _send(room_id, f"Couldn't process that PDF: {e}")
    finally:
        if 'local_path' in locals() and os.path.exists(local_path):
            os.unlink(local_path)


async def on_image(room: MatrixRoom, event: RoomMessageImage):
    """Handle unencrypted image messages."""
    if event.sender == client.user_id or not _first_sync_done:
        return
    user_name = _matrix_user_to_name(event.sender)
    room_id = room.room_id
    filename = event.body or "image.jpg"
    log.info(f"Image from {user_name}: {filename}")

    try:
        local_path = await _download_matrix_file(event.url, filename)
        await _handle_receipt_image(room_id, user_name, local_path)
    except Exception as e:
        log.error(f"Failed to process image: {e}")
        await _send(room_id, f"Couldn't process that image: {e}")
    finally:
        if 'local_path' in locals() and os.path.exists(local_path):
            os.unlink(local_path)


async def on_file(room: MatrixRoom, event: RoomMessageFile):
    """Handle unencrypted file messages."""
    if event.sender == client.user_id or not _first_sync_done:
        return
    user_name = _matrix_user_to_name(event.sender)
    room_id = room.room_id
    filename = event.body or "file"

    if not filename.lower().endswith(".pdf"):
        log.debug(f"Ignoring non-PDF file: {filename}")
        return

    log.info(f"PDF from {user_name}: {filename}")
    await _send(room_id, f"Got **{filename}** -- processing...")

    try:
        local_path = await _download_matrix_file(event.url, filename)
        await _handle_pdf(room_id, user_name, local_path)
    except Exception as e:
        log.error(f"Failed to process PDF: {e}")
        await _send(room_id, f"Couldn't process that PDF: {e}")
    finally:
        if 'local_path' in locals() and os.path.exists(local_path):
            os.unlink(local_path)


async def on_message(room: MatrixRoom, event: RoomMessageText):
    """Handle incoming text messages."""
    global _first_sync_done

    # Ignore messages from ourselves
    if event.sender == client.user_id:
        return

    # Ignore messages from before we started (initial sync)
    if not _first_sync_done:
        return

    text = event.body.strip()
    if not text:
        return

    sender = event.sender
    user_name = _matrix_user_to_name(sender)
    room_id = room.room_id

    # Check for pending receipt/statement confirmation
    if room_id in _pending_receipts:
        lower_confirm = text.lower().strip()
        confirm_words = ("yes", "y", "log it", "log them", "yep", "yeah", "do it", "go ahead", "sure")
        deny_words = ("no", "n", "nah", "nope", "cancel", "skip", "don't", "nevermind")
        is_confirm = any(w in lower_confirm.split() or lower_confirm.startswith(w) for w in confirm_words)
        is_deny = any(w in lower_confirm.split() or lower_confirm.startswith(w) for w in deny_words)
        if is_confirm and not is_deny:
            pending = _pending_receipts.pop(room_id)
            try:
                import finance as _fin
                if pending.get("type") == "bank_statement":
                    logged_w = 0
                    logged_d = 0
                    for tx in pending.get("transactions", []):
                        tx_type = tx.get("type", "withdrawal")
                        _fin.log_transaction(
                            user_name, tx.get("description", "Unknown"),
                            tx.get("amount", 0), category=tx.get("category", "Other"),
                            tx_type=tx_type, tx_date=tx.get("date"),
                        )
                        if tx_type == "deposit":
                            logged_d += 1
                        else:
                            logged_w += 1
                    # Also log to Firefly if enabled
                    if firefly:
                        acct_id = pending.get("account_id", 1)
                        acct_type = pending.get("account_type", "asset")
                        for tx in pending.get("transactions", []):
                            try:
                                firefly.log_transaction(
                                    description=tx.get("description", "Unknown"),
                                    amount=tx.get("amount", 0),
                                    category=tx.get("category", "Other"),
                                    destination_name=tx.get("description", "Unknown"),
                                    tx_date=tx.get("date"),
                                    tx_type=tx.get("type", "withdrawal"),
                                    source_id=acct_id, account_type=acct_type,
                                )
                            except Exception:
                                pass
                    acct_name = pending.get("account", "Unknown")
                    await _send(room_id, f"Logged {logged_w} withdrawals and {logged_d} deposits from **{acct_name}**.")
                else:
                    _fin.log_receipt(user_name, pending["store"], pending["total"])
                    if firefly:
                        try:
                            firefly.log_receipt(store=pending["store"], total=pending["total"], items=pending.get("items"))
                        except Exception:
                            pass
                    await _send(room_id, f"Logged ${pending['total']:.2f} at {pending['store']}.")
            except Exception as e:
                log.error(f"Firefly log failed: {e}")
                await _send(room_id, f"Failed to log to Firefly: {e}")
            return
        elif is_deny:
            _pending_receipts.pop(room_id)
            await _send(room_id, "No problem, skipped.")
            return

    # Check for pending Gmail auth code
    if sender in _pending_gmail_auth:
        code = text.strip()
        if len(code) > 10:  # auth codes are long
            try:
                import gmail_client as _gmail
                if _gmail.exchange_auth_code(user_name, code):
                    _pending_gmail_auth.discard(sender)
                    await _send(room_id, "Gmail connected! I'll start scanning your email at 8 AM daily. You can also ask me to check your email anytime.")
                    log.info(f"Gmail setup complete for {user_name}")
                else:
                    await _send(room_id, "That code didn't work. Try the link again and paste the new code.")
            except Exception as e:
                log.error(f"Gmail auth failed for {user_name}: {e}")
                await _send(room_id, "Something went wrong with the authorization. Let's try again — say 'set up my email' to start over.")
                _pending_gmail_auth.discard(sender)
            return

    # Check for pending email setup flow
    if sender in _pending_email_setup:
        handled = await _handle_email_setup_flow(room_id, text, sender)
        if handled:
            return

    # Check for commands (! prefix or / prefix)
    if text.startswith("!") or text.startswith("/"):
        parts = text[1:].split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        handler = COMMANDS.get(cmd)
        if handler:
            try:
                await handler(room_id, args, room, sender, user_name)
            except Exception as e:
                log.error(f"Command error ({cmd}): {e}")
                await _send(room_id, f"Something went wrong with that command.")
            return

    # Don't respond to short acknowledgements
    lower = text.lower().strip()
    ack_words = {"ok", "okay", "k", "thanks", "thank you", "thx", "ty",
                 "cool", "got it", "sure", "yep", "yup", "np", "alright",
                 "sounds good", "perfect", "great", "nice", "good"}
    if lower.rstrip("!.,") in ack_words:
        return

    # Check for add intent BEFORE list triggers — "add X to the grocery list" should add, not show list
    add_patterns = ("add ", "put ", "we need ", "can you add ", "please add ", "could you add ")
    if db:
        for pat in add_patterns:
            if lower.startswith(pat) or f" {pat}" in lower:
                # Let AI handle it — it's better at extracting the item name from natural language
                break
        else:
            # No add intent found — check for list triggers
            list_triggers = ("what do we need", "grocery list", "shopping list", "what's on the list",
                             "show me the list", "what do we have to buy", "what do we need to buy",
                             "what's on the grocery", "show the list", "what we need",
                             "can you show me the grocery", "send me the list", "send the list",
                             "what are we getting", "what should we get")
            if any(trigger in lower for trigger in list_triggers):
                await cmd_list(room_id)
                return

    if db and (lower.startswith("bought ") or lower.startswith("got ")):
        item = text.split(" ", 1)[1].strip()
        if item and db.mark_item_bought(item, bought_by=user_name):
            await _send(room_id, f"Marked {item} as bought!")
            return

    # Skip AI response if a file is being processed in this room (text + file arrive separately)
    import time as _time
    file_time = _recent_file_rooms.get(room_id, 0)
    if _time.time() - file_time < 30:
        log.debug(f"Skipping AI response — file being processed in {room_id}")
        return

    # Group chat engagement scoring — should Frank respond?
    is_dm = _is_private(room)
    should_respond, score = humanize.should_respond_in_group(text, room_id, is_dm)
    if not should_respond:
        return

    # Fall through to AI — use batcher to collect rapid-fire messages
    await _batcher.add(
        chat_id=room_id,
        message=text,
        user_name=user_name,
        callback=_handle_ai_message,
        room_id=room_id,
        room=room,
        sender=sender,
    )


async def _handle_ai_message(text: str, user_name: str, room_id: str,
                              room: MatrixRoom, sender: str):
    """Process a (possibly batched) message through AI with human-like timing."""
    try:
        is_dm = _is_private(room)
        chat_id = f"matrix_{room_id}"

        # Show typing indicator while AI thinks
        try:
            await client.room_typing(room_id, typing_state=True, timeout=30000)
        except Exception:
            pass

        reply = None
        actions = []

        # Check for ULTRAPLAN (complex planning requests)
        if ultraplan.should_ultraplan(text):
            await _send(room_id, "Let me think about that properly...")
            try:
                await client.room_typing(room_id, typing_state=True, timeout=60000)
            except Exception:
                pass
            plan_result = await asyncio.get_event_loop().run_in_executor(
                None, ultraplan.run_plan, text, "", user_name
            )
            if plan_result:
                reply = plan_result

        # Check for parallel coordinator (multi-source requests)
        elif coordinator.should_use_parallel(text):
            tasks = coordinator.get_full_status_tasks()
            results = await coordinator.run_parallel(tasks)
            combined = coordinator.build_combined_context(results)
            # Feed combined context to AI for a coherent summary
            result = ai.handle_message(
                text, user_name=user_name, is_private=is_dm,
                chat_id=chat_id, extra_context=combined
            )
            reply = result["reply"]
            actions = result.get("actions", [])

        # Normal AI handling
        if reply is None:
            result = ai.handle_message(text, user_name=user_name, is_private=is_dm, chat_id=chat_id)
            reply = result["reply"]
            actions = result.get("actions", [])

        for action in actions:
            act = action.get("action")
            item = action.get("item", "")
            if act == "add" and item and db:
                added, existing = db.add_shopping_item(item, requested_by=user_name)
                if not added and existing.lower() not in reply.lower():
                    reply = reply.rstrip() + f"\n\n('{existing}' is already on the list — want me to add a different brand/variety too?)"
            elif act == "bought" and item and db:
                db.mark_item_bought(item, bought_by=user_name)
                db.record_event(item, "bought", note=f"Bought by {user_name}")
            elif act == "remove" and item and db:
                db.remove_shopping_item(item)
            elif act in ("remind", "timer"):
                time_str = action.get("time", "")
                msg = action.get("message", "") or "Timer done!"
                remind_text = time_str + " " + msg
                remind_at, remind_msg = reminders.parse_reminder_time(remind_text)
                if remind_at:
                    reminders.add_reminder(user_name, sender, remind_msg or msg, remind_at)
                    log.info(f"Reminder/timer set for {user_name} at {remind_at}: {remind_msg or msg}")
                elif time_str:
                    # Fallback: try parsing time alone
                    import re
                    m = re.search(r'(\d+)\s*(min|hour|hr)', time_str.lower())
                    if m:
                        val = int(m.group(1))
                        unit = m.group(2)
                        delta = timedelta(minutes=val) if "min" in unit else timedelta(hours=val)
                        reminders.add_reminder(user_name, sender, msg, datetime.now() + delta)
                        log.info(f"Timer set for {user_name} in {val} {unit}: {msg}")
                    else:
                        reminders.add_reminder(user_name, sender, msg, datetime.now() + timedelta(hours=1))
                else:
                    reminders.add_reminder(user_name, sender, msg,
                                          datetime.now() + timedelta(hours=1))
            elif act == "send_message":
                to_user = action.get("to", "").lower()
                msg_text = action.get("message", "")
                if to_user in config.FAMILY_MEMBERS and msg_text:
                    target_matrix_id = config.FAMILY_MEMBERS[to_user].get("matrix_id")
                    if target_matrix_id:
                        try:
                            await _send_to_user(target_matrix_id, msg_text)
                            log.info(f"Matrix DM sent to {to_user}: {msg_text[:50]}")
                        except Exception as e:
                            log.error(f"Failed to send Matrix DM to {to_user}: {e}")
            elif act == "log_spend":
                store = action.get("store", "Unknown")
                amount = action.get("amount", 0)
                if amount:
                    if db:
                        db.log_spend(store, float(amount))
                    try:
                        import finance
                        finance.log_receipt(user_name, store, float(amount))
                    except Exception as e:
                        log.warning(f"Built-in finance log failed: {e}")
                    if firefly:
                        try:
                            firefly.log_receipt(store, float(amount))
                        except Exception as e:
                            log.warning(f"Firefly log failed: {e}")
            elif act == "send_email":
                to_addr = action.get("to", "")
                subject = action.get("subject", "")
                body = action.get("body", "")
                if to_addr and subject and body and agentmail_client:
                    try:
                        agentmail_client.send_email(to_addr, subject, body)
                        log.info(f"Email sent to {to_addr}: {subject}")
                    except Exception as e:
                        log.error(f"Email send failed: {e}")
            elif act == "search_photos":
                query = action.get("query", "")
                start_date = action.get("start_date", "")
                end_date = action.get("end_date", "")
                if immich_client and (query or start_date):
                    if start_date:
                        results = immich_client.search_by_date(start_date, end_date or None, limit=3)
                        label = f"Photos from {start_date}" + (f" to {end_date}" if end_date else "")
                    else:
                        results = immich_client.search_photos(query, limit=3)
                        label = f"Photo: {query}"
                    if results:
                        path = immich_client.download_thumbnail(results[0]["id"])
                        if path:
                            await _send_image(room_id, path, label)
            elif act == "track_debt":
                try:
                    import debts
                    creditor = action.get("creditor", "").lower()
                    debtor_name = action.get("debtor", "").lower()
                    amount = float(action.get("amount", 0))
                    desc = action.get("description", "")
                    if creditor and debtor_name and amount > 0:
                        debts.add_debt(creditor, debtor_name, amount, desc)
                        log.info(f"Debt tracked: {debtor_name} owes {creditor} ${amount:.2f}")
                except Exception as e:
                    log.error(f"Debt tracking failed: {e}")
            elif act == "settle_debt":
                try:
                    import debts
                    creditor = action.get("creditor", "").lower()
                    debtor_name = action.get("debtor", "").lower()
                    if creditor and debtor_name:
                        debts.mark_paid(creditor=creditor, debtor=debtor_name)
                        log.info(f"Debt settled: {debtor_name} -> {creditor}")
                except Exception as e:
                    log.error(f"Debt settle failed: {e}")
            elif act == "setup_email":
                await cmd_setup_email(room_id, "", sender, user_name)
            elif act == "plan_dinner":
                meal = action.get("meal", "")
                plan_date = action.get("date", "")
                ingredients = action.get("ingredients", [])
                if meal and plan_date and db:
                    # Check if a saved recipe matches and pull ingredients from it
                    if not ingredients and recipes:
                        try:
                            results = recipes.search_recipes(meal)
                            if results:
                                recipe_data = recipes.get_recipe(results[0]["id"])
                                if recipe_data and recipe_data["ingredients"]:
                                    ingredients = []
                                    for ing in recipe_data["ingredients"]:
                                        parts = []
                                        if ing.get("amount"):
                                            parts.append(str(ing["amount"]))
                                        if ing.get("unit"):
                                            parts.append(ing["unit"])
                                        parts.append(ing["name"])
                                        ingredients.append(" ".join(parts))
                        except Exception:
                            pass
                    db.add_meal_plan(plan_date, meal, ingredients=ingredients, planned_by=user_name)
                    log.info(f"Dinner planned: {meal} on {plan_date} by {user_name}")
            elif act == "clear_dinner":
                meal = action.get("meal", "")
                if meal and db:
                    removed = db.remove_meal_plan(meal_name=meal)
                    log.info(f"Dinner plan removed: {meal} ({removed} plans)")
            elif act == "followup":
                topic = action.get("topic", "")
                question = action.get("question", "")
                hours = action.get("hours", 24)
                if topic and question:
                    episodes.schedule_followup(user_name, topic, question, delay_hours=float(hours))

        # Store episode summary for this interaction
        if reply:
            episodes.store_episode(
                user_name=user_name,
                summary=f"{user_name} said: {text[:100]}. Frank replied about: {reply[:100]}",
                topics=[w for w in text.lower().split() if len(w) > 4][:5],
                chat_id=room_id,
            )

        if reply:
            # Human-like delay before sending
            await humanize.human_delay(text, reply)

            # Stop typing indicator
            try:
                await client.room_typing(room_id, typing_state=False)
            except Exception:
                pass

            # Split long responses into chunks with pauses
            chunks = humanize.chunk_response(reply)
            for i, chunk in enumerate(chunks):
                if i > 0:
                    # Pause between chunks, show typing again
                    try:
                        await client.room_typing(room_id, typing_state=True, timeout=10000)
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    try:
                        await client.room_typing(room_id, typing_state=False)
                    except Exception:
                        pass
                await _send(room_id, chunk)

            humanize.mark_participated(room_id)
            conversation_log.log_interaction(user_name, text, reply)
            conversation_log.extract_and_save_learnings(user_name, text, reply)
            if mem0_memory:
                mem0_memory.add_conversation(user_name, text, reply)

            # Style learning — log interaction and mark previous as engaged
            style_learner.mark_engaged(user_name)  # This message means they engaged with last response
            style_learner.log_interaction(user_name, text, reply)

            # Periodically update profile with LLM
            if style_learner.should_update_profile(user_name):
                try:
                    style_learner.update_profile_with_llm(
                        user_name,
                        lambda prompt: ai._chat(
                            [{"role": "user", "content": prompt}],
                            max_tokens=500,
                        )
                    )
                except Exception as e:
                    log.debug(f"Style profile update failed: {e}")

            # Buddy interaction
            try:
                buddy_result = buddy.interact(user_name)
                buddy_msg = buddy.get_interaction_message(buddy_result, user_name)
                if buddy_msg:
                    await asyncio.sleep(1.0)
                    await _send(room_id, buddy_msg)
            except Exception as e:
                log.debug(f"Buddy error: {e}")
    except Exception as e:
        log.error(f"AI error: {e}")
        try:
            await client.room_typing(room_id, typing_state=False)
        except Exception:
            pass
        await _send(room_id, humanize.get_error_response())


# ─── Scheduled Job Support ───

async def send_to_family_group(text: str):
    """Send a message to the Matrix family group room."""
    if client and config.MATRIX_FAMILY_ROOM_ID:
        await _send(config.MATRIX_FAMILY_ROOM_ID, text)


async def send_to_user_by_name(name: str, text: str):
    """Send a DM to a family member by name."""
    member = config.FAMILY_MEMBERS.get(name.lower())
    if member and member.get("matrix_id") and client:
        await _send_to_user(member["matrix_id"], text)


# ─── External API for other services ───

async def send_alert(text: str, room_id: str = None, image_b64: str = None):
    """Send an alert message to a Matrix room. Used by external services like UniFi webhook."""
    target_room = room_id or config.MATRIX_FAMILY_ROOM_ID
    if not client or not target_room:
        return

    if image_b64:
        try:
            import base64
            # Strip data URI prefix if present
            if "," in image_b64[:50]:
                image_b64 = image_b64.split(",", 1)[1]
            image_b64 = image_b64.strip().replace("\n", "").replace("\r", "").replace(" ", "")
            img_data = base64.b64decode(image_b64)

            # Upload to Synapse
            resp, _ = await client.upload(
                io.BytesIO(img_data),
                content_type="image/jpeg",
                filename="alert.jpg",
                filesize=len(img_data),
            )

            if hasattr(resp, "content_uri"):
                # Send image message
                content = {
                    "msgtype": "m.image",
                    "body": text,
                    "url": resp.content_uri,
                    "info": {
                        "mimetype": "image/jpeg",
                        "size": len(img_data),
                    },
                }
                await client.room_send(target_room, "m.room.message", content)
                # Also send text caption
                await _send(target_room, text)
                return
        except Exception as e:
            log.warning(f"Matrix image upload failed: {e}, falling back to text")

    await _send(target_room, text)


# ─── Reminder Delivery ───

async def deliver_matrix_reminders():
    """Check for due reminders and deliver via Matrix if the ID is a Matrix user."""
    due = reminders.get_due_reminders()
    for r in due:
        target = r["telegram_id"]  # field name is legacy, stores Matrix ID too
        if target.startswith("@") and ":" in target:
            # This is a Matrix user ID
            try:
                msg = f"Hey {r['user_name'].title()}! Reminder:\n\n{r['message']}"
                await _send_to_user(target, msg)
                reminders.mark_delivered(r["id"])
                log.info(f"Matrix reminder delivered to {r['user_name']}: {r['message']}")
            except Exception as e:
                log.error(f"Matrix reminder delivery failed: {e}")


# ─── Client Lifecycle ───

async def start(loop=None):
    """Start the Matrix client. Call from the main event loop."""
    global client, _first_sync_done

    homeserver = config.MATRIX_HOMESERVER
    user_id = config.MATRIX_BOT_USER
    password = os.environ.get("MATRIX_BOT_PASSWORD", "")

    if not password:
        log.warning("MATRIX_BOT_PASSWORD not set, Matrix client disabled")
        return

    # E2E encryption store
    store_path = Path.home() / "family-bot" / "matrix_store"
    store_path.mkdir(parents=True, exist_ok=True)

    # Persist device_id so we reuse the same one across restarts
    device_id_file = store_path / "device_id"
    saved_device_id = None
    if device_id_file.exists():
        saved_device_id = device_id_file.read_text().strip()
        log.info(f"Reusing saved device ID: {saved_device_id}")

    client_config = AsyncClientConfig(
        store_name="matrix_store",
        store_sync_tokens=True,
        encryption_enabled=True,
    )

    client = AsyncClient(
        homeserver,
        user_id,
        device_id=saved_device_id,
        store_path=str(store_path),
        config=client_config,
    )

    # Login (device_id is set on the client constructor)
    resp = await client.login(password, device_name="FrankBot")
    if not isinstance(resp, LoginResponse):
        log.error(f"Matrix login failed: {resp}")
        return

    # Save device_id for next restart
    if not saved_device_id or saved_device_id != resp.device_id:
        device_id_file.write_text(resp.device_id)
        log.info(f"Saved new device ID: {resp.device_id}")

    log.info(f"Matrix logged in as {resp.user_id} (device: {resp.device_id})")

    # Upload encryption keys to server so other clients can encrypt for us
    if client.should_upload_keys:
        key_resp = await client.keys_upload()
        log.info(f"Uploaded encryption keys: {getattr(key_resp, 'signed_curve25519_count', '?')} one-time keys")

    # Debug: catch-all event callback
    async def on_any_event(room, event):
        log.info(f"Matrix event: {type(event).__name__} from {getattr(event, 'sender', '?')} in {room.display_name}")

    client.add_event_callback(on_any_event, Event)

    # Register message callbacks
    client.add_event_callback(on_message, RoomMessageText)
    client.add_event_callback(on_image, RoomMessageImage)
    client.add_event_callback(on_file, RoomMessageFile)
    client.add_event_callback(on_encrypted_image, RoomEncryptedImage)
    client.add_event_callback(on_encrypted_file, RoomEncryptedFile)

    # Auto-join on invite
    async def on_invite(room, event):
        log.info(f"Matrix invite to {room.room_id}, auto-joining")
        await client.join(room.room_id)

    # Handle undecryptable messages
    async def on_megolm(room, event):
        log.warning(f"Undecryptable message from {event.sender} in {room.display_name}: {event.session_id}")

    from nio import InviteMemberEvent
    client.add_event_callback(on_invite, InviteMemberEvent)
    client.add_event_callback(on_megolm, MegolmEvent)

    # Do initial sync to catch up (we ignore messages from this)
    await client.sync(timeout=10000, full_state=True)
    _first_sync_done = True
    log.info("Matrix initial sync done, now listening for messages")

    # Accept any pending invites from initial sync
    for room_id in list(client.invited_rooms.keys()):
        log.info(f"Auto-joining pending invite: {room_id}")
        await client.join(room_id)

    # Trust all known devices
    _trust_all_devices()

    # Auto-join invited rooms
    if hasattr(client, 'rooms'):
        log.info(f"Matrix joined {len(client.rooms)} rooms")

    # Start sync loop
    log.info("Matrix sync loop starting")
    while True:
        try:
            resp = await client.sync(timeout=30000)
            log.debug(f"Matrix sync: {resp.next_batch[:20] if hasattr(resp, 'next_batch') else resp}")

            # Upload keys if needed (replenish one-time keys)
            if client.should_upload_keys:
                await client.keys_upload()

            # Trust any new devices we discover
            _trust_all_devices()
        except Exception as e:
            log.error(f"Matrix sync error: {e}", exc_info=True)
            await asyncio.sleep(5)


async def stop():
    """Gracefully stop the Matrix client."""
    global client
    if client:
        await client.close()
        log.info("Matrix client stopped")
