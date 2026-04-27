#!/usr/bin/env python3
"""
Family Bot (Matrix) - Grocery, finance, and household management for the family.
Standalone Matrix service — no Telegram dependency.
"""
import asyncio
import logging
import json
from datetime import datetime, date
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.jobstores.base import JobLookupError

_BOT_DIR = Path(__file__).parent
_JOBS_DB = _BOT_DIR / "jobs.db"
_CHECKIN_SCHEDULE_STATE = _BOT_DIR / ".checkin_schedule_state.json"
_CHECKIN_SENT_STATE = _BOT_DIR / ".checkin_sent_state.json"
_CHECKIN_DEDUP_HOURS = 18

import config
import briefing
import conversation_log
import reminders
import episodes
import dream
import checkin
import matrix_client
import debts

# Optional modules
db = None
firefly = None
email_client = None
voice_api = None

telegram_client = None

if config.GROCERY_ENABLED:
    import db
if config.FIREFLY_ENABLED:
    import firefly
if config.EMAIL_ENABLED:
    import email_client
gmail_client = None
if config.GMAIL_ENABLED:
    import gmail_client
if config.VOICE_ENABLED:
    import voice_api
if config.TELEGRAM_ENABLED:
    import telegram_client

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
    msg = await asyncio.to_thread(briefing.build_briefing)
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

    # Add dinner plan ingredients
    try:
        plans = db.get_meal_plan_ingredients(upcoming_only=True)
        for p in plans:
            if p["ingredients"]:
                lines.append(f"\n🍽️ FOR {p['date']} — {p['meal'].upper()}")
                for ing in p["ingredients"]:
                    lines.append(f"  {ing}")
    except Exception:
        pass

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


async def _scan_email_for_member(member_name, client_mod, use_gmail=False):
    """Scan one member's email for bills and e-transfers."""
    nickname = config.FAMILY_MEMBERS.get(member_name, {}).get("nickname", member_name.title())
    try:
        if use_gmail:
            bills_raw = client_mod.get_bills(limit=5, member_name=member_name)
            # Gmail returns snippet instead of body_preview
            bills = [{"subject": b.get("subject", ""), "body_preview": b.get("snippet", ""),
                       "from": b.get("from", "")} for b in bills_raw]
        else:
            bills = client_mod.get_bills(limit=5, member_name=member_name)

        new_bills = []
        for b in bills:
            parsed = email_client.parse_bill_email(b["subject"], b["body_preview"], b["from"])
            if parsed and parsed.get("amount"):
                new_bills.append(parsed)
                try:
                    payee = parsed["payee"]
                    cat_map = {
                        "hydro": "Electricity",
                        "enbridge": "Gas & Heating",
                        "union gas": "Gas & Heating",
                        "rogers": "Internet",
                        "insurance": "Insurance",
                    }
                    category = "Bills"
                    for key, cat in cat_map.items():
                        if key in payee.lower():
                            category = cat
                            break
                    # Log to built-in finance (per-user)
                    try:
                        import finance
                        finance.log_transaction(member_name, payee, parsed["amount"], category=category)
                    except Exception as e:
                        log.warning(f"Built-in finance log failed for {member_name}: {e}")
                    # Also log to Firefly if enabled
                    if firefly:
                        firefly.log_transaction(
                            description=payee,
                            amount=parsed["amount"],
                            category=category,
                            destination_name=payee,
                        )
                except Exception as e:
                    log.warning(f"Bill log failed for {member_name}: {e}")

        if new_bills:
            lines = [f"BILL ALERT\n"]
            for b in new_bills:
                lines.append(f"  {b['payee']}: ${b['amount']:.2f}")
                if b.get("due_date"):
                    lines.append(f"  Due: {b['due_date']}")
                lines.append("")
            await matrix_client.send_to_user_by_name(member_name, "\n".join(lines))
            log.info(f"Bill scan for {member_name}: {len(new_bills)} bills found")

        # Check for e-transfers (only for IMAP — Gmail uses snippet which may not have enough detail)
        if not use_gmail:
            try:
                recent = client_mod.get_recent("INBOX", limit=10, member_name=member_name)
                for msg in recent:
                    subj = (msg.get("subject") or "").lower()
                    body = msg.get("body_preview", "")
                    if "e-transfer" in subj or "interac" in subj or "etransfer" in subj:
                        import re
                        amount_match = re.search(r'\$\s*([\d,]+\.?\d*)', body)
                        if not amount_match:
                            continue
                        amount = float(amount_match.group(1).replace(",", ""))
                        for name, member in config.FAMILY_MEMBERS.items():
                            nick = member["nickname"].lower()
                            if nick in body.lower() or name in body.lower():
                                settled = debts.settle_by_etransfer(name, amount)
                                if settled:
                                    await matrix_client.send_to_user_by_name(
                                        settled["creditor"],
                                        f"Heads up — {member['nickname']} just sent ${amount:.2f} via e-transfer. "
                                        f"I've marked that debt as settled."
                                    )
                                    log.info(f"E-transfer auto-settled: {name} -> {settled['creditor']} ${amount:.2f}")
                                break
            except Exception as e:
                log.warning(f"E-transfer check failed for {member_name}: {e}")

    except Exception as e:
        log.error(f"Bill scan error for {member_name}: {e}")



async def job_email_cleanup():
    """9:00 AM - Delete junk emails from configured senders."""
    if not email_client or not config.JUNK_SENDERS:
        return
    try:
        deleted = email_client.delete_by_senders(config.JUNK_SENDERS, member_name=config.OWNER)
        if deleted > 0:
            log.info(f"Email cleanup: deleted {deleted} junk emails")
            await matrix_client.send_to_user_by_name(config.OWNER, f"Cleaned up {deleted} junk emails from your inbox.")
    except Exception as e:
        log.error(f"Email cleanup error: {e}")


async def job_bill_scan():
    """8:00 AM - Scan all configured email accounts for bills."""
    scanned = set()

    # Scan per-member email configs
    for member_name, member in config.FAMILY_MEMBERS.items():
        em = member.get("email")
        if not em:
            continue
        if em["type"] == "gmail" and gmail_client:
            await _scan_email_for_member(member_name, gmail_client, use_gmail=True)
            scanned.add(member_name)
        elif em["type"] == "imap" and email_client:
            await _scan_email_for_member(member_name, email_client, use_gmail=False)
            scanned.add(member_name)

    # Fallback: if no per-member configs, use global email settings for owner
    if not scanned and email_client:
        await _scan_email_for_member(config.OWNER, email_client, use_gmail=False)
    elif not scanned and gmail_client:
        await _scan_email_for_member(config.OWNER, gmail_client, use_gmail=True)


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

    # Check for due debt reminders
    try:
        due_debts = debts.get_due_reminders()
        for d in due_debts:
            creditor_nick = config.FAMILY_MEMBERS.get(d["creditor"], {}).get("nickname", d["creditor"].title())
            debtor_nick = config.FAMILY_MEMBERS.get(d["debtor"], {}).get("nickname", d["debtor"].title())
            desc = f" for {d['description']}" if d.get("description") else ""
            msg = (
                f"Hey — {creditor_nick}'s still waiting on that "
                f"${d['amount']:.2f}{desc}. Can you settle up when you get a chance?"
            )
            await matrix_client.send_to_user_by_name(d["debtor"], msg)
            debts.advance_reminder(d["id"])
            log.info(f"Debt reminder sent to {d['debtor']}: ${d['amount']:.2f} owed to {d['creditor']}")
    except Exception as e:
        log.warning(f"Debt reminder check failed: {e}")

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



# Scheduler reference for adding one-off jobs
_scheduler = None


def _load_json_state(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_json_state(path, data):
    try:
        path.write_text(json.dumps(data))
    except OSError as e:
        log.warning("Failed to write %s: %s", path.name, e)


async def _do_checkin(name, msg):
    """Send a scheduled check-in DM, with 18h send-side dedup.

    Top-level (not a closure) so apscheduler's SQLAlchemyJobStore can
    pickle the function reference for restart-safe persistence.
    """
    state = _load_json_state(_CHECKIN_SENT_STATE)
    last_sent = state.get(name)
    if last_sent:
        try:
            last_dt = datetime.fromisoformat(last_sent)
            elapsed = (datetime.now() - last_dt).total_seconds()
            if elapsed < _CHECKIN_DEDUP_HOURS * 3600:
                log.warning(
                    "Skipping check-in for %s — already sent at %s (%.1fh ago, <%dh dedup window)",
                    name, last_sent, elapsed / 3600, _CHECKIN_DEDUP_HOURS,
                )
                return
        except ValueError:
            pass

    await matrix_client.send_to_user_by_name(name, msg)
    log.info("Check-in sent to %s", name)
    state[name] = datetime.now().isoformat()
    _write_json_state(_CHECKIN_SENT_STATE, state)


async def job_schedule_checkins():
    """7:00 AM - Schedule random check-in DMs for today (idempotent per calendar day)."""
    if not _scheduler:
        log.warning("Scheduler not ready; cannot schedule check-ins")
        return

    today = date.today().isoformat()
    state = _load_json_state(_CHECKIN_SCHEDULE_STATE)
    if state.get("date") == today:
        log.info("Check-ins already scheduled for %s — skipping re-schedule", today)
        return

    schedule = checkin.get_random_hours()
    now = datetime.now()
    for name, hour, minute, msg in schedule:
        run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_at <= now:
            log.info(
                "Skipping check-in for %s — random time %02d:%02d already passed",
                name, hour, minute,
            )
            continue

        job_id = f"checkin_{name}"
        try:
            _scheduler.remove_job(job_id)
        except JobLookupError:
            pass
        except Exception as e:
            log.warning("remove_job(%s) failed: %s", job_id, e)

        _scheduler.add_job(
            _do_checkin,
            DateTrigger(run_date=run_at),
            args=[name, msg],
            id=job_id,
            replace_existing=True,
        )
        log.info("Scheduled check-in for %s at %02d:%02d", name, hour, minute)

    _write_json_state(
        _CHECKIN_SCHEDULE_STATE,
        {"date": today, "scheduled_at": now.isoformat()},
    )

# ─── Main ───

async def async_main():
    scheduler = AsyncIOScheduler(
        timezone=config.TIMEZONE,
        jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{_JOBS_DB}")},
    )
    scheduler.add_job(job_morning_briefing, CronTrigger(hour=6, minute=30), id="morning_briefing", replace_existing=True)
    scheduler.add_job(job_grocery_push, CronTrigger(hour=9, minute=0), id="grocery_push", replace_existing=True)
    scheduler.add_job(job_low_stock_alert, CronTrigger(hour=18, minute=0), id="low_stock_alert", replace_existing=True)
    scheduler.add_job(job_bill_scan, CronTrigger(hour=8, minute=0), id="bill_scan", replace_existing=True)
    scheduler.add_job(job_email_cleanup, CronTrigger(hour=9, minute=0), id="email_cleanup", replace_existing=True)
    scheduler.add_job(job_payment_reminders, CronTrigger(hour=10, minute=0), id="payment_reminders", replace_existing=True)
    scheduler.add_job(job_check_reminders, IntervalTrigger(minutes=1), id="check_reminders", replace_existing=True)
    scheduler.add_job(job_daily_log, CronTrigger(hour=23, minute=0), id="daily_log", replace_existing=True)
    scheduler.add_job(job_dream, CronTrigger(hour=2, minute=0), id="dream", replace_existing=True)
    scheduler.add_job(job_schedule_checkins, CronTrigger(hour=7, minute=0), id="schedule_checkins", replace_existing=True)
    global _scheduler
    _scheduler = scheduler
    scheduler.start()
    log.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    # Start voice API (optional)
    if voice_api:
        await voice_api.start_voice_api()

    # Start Telegram bot (optional)
    if telegram_client:
        await telegram_client.start()
        log.info("Telegram bot starting")

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
        if telegram_client:
            await telegram_client.stop()
        await matrix_client.stop()


def main():
    log.info("Family Bot (Matrix) starting...")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
