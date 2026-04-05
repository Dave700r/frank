#!/usr/bin/env python3
"""
Family Bot (Matrix) - Grocery, finance, and household management for the family.
Standalone Matrix service — no Telegram dependency.
"""
import asyncio
import logging
import json
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
import briefing
import conversation_log
import reminders
import episodes
import dream
import matrix_client

# Optional modules
db = None
firefly = None
email_client = None
voice_api = None

if config.GROCERY_ENABLED:
    import db
if config.FIREFLY_ENABLED:
    import firefly
if config.EMAIL_ENABLED:
    import email_client
if config.VOICE_ENABLED:
    import voice_api

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path.home() / "family-bot.log"),
    ],
)
log = logging.getLogger("family-bot")


# ─── Scheduled Jobs (Matrix only) ───

async def job_morning_briefing():
    """6:30 AM - Morning briefing to family group."""
    msg = briefing.build_briefing()
    await matrix_client.send_to_family_group(msg)
    log.info("Morning briefing sent (Matrix)")


async def job_grocery_push():
    """9:00 AM - Push shopping list to family group."""
    if not db:
        return
    items = db.get_shopping_list()
    if not items:
        return

    cat_emoji = {
        "PRODUCE": "🥬", "DAIRY": "🥛", "MEAT": "🥩", "BAKERY": "🍞",
        "PANTRY": "🥫", "FROZEN": "🧊", "HOUSEHOLD": "🧹", "PET": "🐕",
        "OTHER": "📦",
    }

    by_cat = {}
    for item in items:
        cat = (item["category"] or "other").upper()
        by_cat.setdefault(cat, []).append(f"  {item['name']}")

    lines = ["SHOPPING LIST\n"]
    for cat, entries in sorted(by_cat.items()):
        emoji = cat_emoji.get(cat, "📦")
        lines.append(f"{emoji} {cat}")
        lines.extend(entries)
        lines.append("")
    lines.append(f"{len(items)} items — reply !bought <item> when you pick something up!")

    await matrix_client.send_to_family_group("\n".join(lines))
    log.info(f"Grocery list pushed ({len(items)} items)")


async def job_low_stock_alert():
    """6:00 PM - Alert about low stock items."""
    if not db:
        return
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

    await matrix_client.send_to_family_group("\n".join(lines))
    log.info("Low stock alert sent")


async def job_bill_scan():
    """8:00 AM - Scan for new bills, log to Firefly if parseable."""
    if not email_client:
        return
    try:
        bills = email_client.get_bills(limit=5)
        new_bills = []
        for b in bills:
            parsed = email_client.parse_bill_email(b["subject"], b["body_preview"], b["from"])
            if parsed and parsed.get("amount"):
                new_bills.append(parsed)
                try:
                    payee = parsed["payee"]
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
                    if firefly:
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
            await matrix_client.send_to_user_by_name(config.OWNER, "\n".join(lines))
            log.info(f"Bill scan: {len(new_bills)} bills found and logged")
    except Exception as e:
        log.error(f"Bill scan error: {e}")


async def job_payment_reminders():
    """10:00 AM - Check payment trackers and DM owner."""
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
                await matrix_client.send_to_user_by_name(config.OWNER, msg)
                log.info(f"Payment reminder sent: {tracker.name}")
        except (json.JSONDecodeError, KeyError) as e:
            log.warning(f"Bad payment tracker {tracker.name}: {e}")


async def job_check_reminders():
    """Every minute - check for due reminders and deliver them."""
    try:
        await matrix_client.deliver_matrix_reminders()
    except Exception as e:
        log.warning(f"Matrix reminder check failed: {e}")

    # Check for due follow-ups
    try:
        due = episodes.get_due_followups()
        for fu in due:
            await matrix_client.send_to_user_by_name(fu["user_name"], fu["question"])
            episodes.mark_followup_delivered(fu["id"])
            log.info(f"Follow-up delivered to {fu['user_name']}: {fu['question'][:50]}")
    except Exception as e:
        log.warning(f"Follow-up check failed: {e}")


async def job_daily_log():
    """11:00 PM - Write daily conversation log to disk."""
    conversation_log.write_daily_log()


async def job_dream():
    """2:00 AM - Memory consolidation (runs only if 3-gate trigger passes)."""
    import ai
    def ai_consolidate(prompt):
        return ai._chat(
            messages=[{"role": "user", "content": prompt}],
            system="You are Frank's memory consolidation system. Summarize and consolidate conversation memories concisely.",
            max_tokens=300,
        )
    await dream.dream(ai_fn=ai_consolidate)


# ─── Main ───

async def async_main():
    # Start scheduler
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    scheduler.add_job(job_morning_briefing, CronTrigger(hour=6, minute=30), id="morning_briefing")
    scheduler.add_job(job_grocery_push, CronTrigger(hour=9, minute=0), id="grocery_push")
    scheduler.add_job(job_low_stock_alert, CronTrigger(hour=18, minute=0), id="low_stock_alert")
    scheduler.add_job(job_bill_scan, CronTrigger(hour=8, minute=0), id="bill_scan")
    scheduler.add_job(job_payment_reminders, CronTrigger(hour=10, minute=0), id="payment_reminders")
    scheduler.add_job(job_check_reminders, IntervalTrigger(minutes=1), id="check_reminders")
    scheduler.add_job(job_daily_log, CronTrigger(hour=23, minute=0), id="daily_log")
    scheduler.add_job(job_dream, CronTrigger(hour=2, minute=0), id="dream")
    scheduler.start()
    log.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    # Start voice API (optional)
    if voice_api:
        await voice_api.start_voice_api()

    # Start Matrix client
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


def main():
    log.info("Family Bot (Matrix) starting...")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
