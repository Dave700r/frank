"""Reminder system for family members."""
import sqlite3
import logging
from datetime import datetime, timedelta

import config

log = logging.getLogger("family-bot.reminders")

DB_PATH = config.REMINDERS_DB


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_name TEXT NOT NULL,
        telegram_id TEXT NOT NULL,
        message TEXT NOT NULL,
        remind_at TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        delivered INTEGER DEFAULT 0,
        delivered_at TEXT
    )""")
    conn.commit()
    return conn


def add_reminder(user_name, telegram_id, message, remind_at):
    """Add a reminder. remind_at is a datetime object."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO reminders (user_name, telegram_id, message, remind_at) VALUES (?, ?, ?, ?)",
        (user_name, telegram_id, message, remind_at.strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()
    conn.close()
    log.info(f"Reminder set for {user_name} at {remind_at}: {message}")
    return True


def get_due_reminders():
    """Get all undelivered reminders that are due now or past due."""
    conn = _get_conn()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = conn.execute(
        "SELECT id, user_name, telegram_id, message, remind_at "
        "FROM reminders WHERE delivered=0 AND remind_at <= ?",
        (now,),
    ).fetchall()
    conn.close()
    return rows


def mark_delivered(reminder_id):
    """Mark a reminder as delivered."""
    conn = _get_conn()
    conn.execute(
        "UPDATE reminders SET delivered=1, delivered_at=datetime('now','localtime') WHERE id=?",
        (reminder_id,),
    )
    conn.commit()
    conn.close()


def get_pending_for_user(user_name):
    """Get all pending reminders for a user."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, message, remind_at FROM reminders "
        "WHERE user_name=? AND delivered=0 ORDER BY remind_at",
        (user_name,),
    ).fetchall()
    conn.close()
    return rows


def cancel_reminder(reminder_id):
    """Cancel (delete) a reminder."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM reminders WHERE id=? AND delivered=0", (reminder_id,))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def parse_reminder_time(text):
    """Parse natural language time expressions into a datetime.
    Returns (datetime, cleaned_message) or (None, None)."""
    import re
    now = datetime.now()
    lower = text.lower()

    # "in X minutes/hours"
    m = re.search(r'in\s+(\d+)\s*(min(?:ute)?s?|hours?|hrs?)', lower)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        if "min" in unit:
            dt = now + timedelta(minutes=val)
        else:
            dt = now + timedelta(hours=val)
        msg = re.sub(r'\s*in\s+\d+\s*(?:min(?:ute)?s?|hours?|hrs?)\s*', ' ', text).strip()
        return dt, msg

    # "at HH:MM" or "at H PM/AM"
    m = re.search(r'at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', lower)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        msg = re.sub(r'\s*at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*', ' ', text).strip()
        return dt, msg

    # "tomorrow"
    if "tomorrow" in lower:
        # Default to 9 AM tomorrow
        dt = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        # Check if there's also a time
        m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', lower)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            ampm = m.group(3)
            if ampm == "pm" and hour < 12:
                hour += 12
            dt = dt.replace(hour=hour, minute=minute)
        msg = re.sub(r'\s*tomorrow\s*', ' ', text)
        msg = re.sub(r'\s*at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*', ' ', msg).strip()
        return dt, msg

    # "tonight"
    if "tonight" in lower:
        dt = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        msg = re.sub(r'\s*tonight\s*', ' ', text).strip()
        return dt, msg

    # "this afternoon"
    if "this afternoon" in lower:
        dt = now.replace(hour=14, minute=0, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        msg = re.sub(r'\s*this afternoon\s*', ' ', text).strip()
        return dt, msg

    return None, None
