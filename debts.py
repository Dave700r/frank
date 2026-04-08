"""Family debt tracking with escalating reminders.

Schedule: reminder on day of, then 2 days later, then daily until paid.
Auto-settles when the debtor confirms payment or an e-transfer is detected."""
import sqlite3
import logging
from datetime import datetime, timedelta, date

import config

log = logging.getLogger("family-bot.debts")

DB_PATH = config.WORKSPACE / "debts.db"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS debts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creditor TEXT NOT NULL,
        debtor TEXT NOT NULL,
        amount REAL NOT NULL,
        description TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        paid INTEGER DEFAULT 0,
        paid_at TEXT,
        reminder_count INTEGER DEFAULT 0,
        last_reminded_at TEXT,
        next_reminder_at TEXT
    )""")
    conn.commit()
    return conn


def _calc_next_reminder(created_at, reminder_count):
    """Calculate when the next reminder should fire.

    Schedule:
      0 reminders sent -> next day at 10 AM
      1 reminder sent  -> 3 days after first reminder
      2+ reminders     -> weekly
    """
    if isinstance(created_at, str):
        created_at = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")

    base_date = created_at.replace(hour=10, minute=0, second=0, microsecond=0)

    if reminder_count == 0:
        # First reminder: next day at 10 AM
        return base_date + timedelta(days=1)
    elif reminder_count == 1:
        # Second reminder: 3 days later
        return base_date + timedelta(days=4)
    else:
        # After that: weekly
        return datetime.now().replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=7)


def add_debt(creditor, debtor, amount, description=None):
    """Record a new debt. Returns the debt ID."""
    conn = _get_conn()
    now = datetime.now()
    next_reminder = _calc_next_reminder(now, 0)
    cur = conn.execute(
        "INSERT INTO debts (creditor, debtor, amount, description, created_at, next_reminder_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (creditor.lower(), debtor.lower(), amount, description,
         now.strftime("%Y-%m-%d %H:%M:%S"), next_reminder.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    debt_id = cur.lastrowid
    conn.close()
    log.info(f"Debt #{debt_id}: {debtor} owes {creditor} ${amount:.2f} ({description})")
    return debt_id


def mark_paid(debt_id=None, creditor=None, debtor=None):
    """Mark a debt as paid. Can match by ID or by creditor+debtor pair (settles most recent)."""
    conn = _get_conn()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if debt_id:
        conn.execute(
            "UPDATE debts SET paid=1, paid_at=? WHERE id=?",
            (now, debt_id),
        )
    elif creditor and debtor:
        # Settle the most recent unpaid debt between these two
        row = conn.execute(
            "SELECT id FROM debts WHERE creditor=? AND debtor=? AND paid=0 "
            "ORDER BY created_at DESC LIMIT 1",
            (creditor.lower(), debtor.lower()),
        ).fetchone()
        if row:
            conn.execute("UPDATE debts SET paid=1, paid_at=? WHERE id=?", (now, row["id"]))
        else:
            conn.close()
            return False
    conn.commit()
    conn.close()
    log.info(f"Debt settled: id={debt_id} creditor={creditor} debtor={debtor}")
    return True


def settle_by_etransfer(sender_name, amount):
    """Try to match an e-transfer to an open debt and settle it.
    Returns the debt dict if matched, None otherwise."""
    conn = _get_conn()
    # Look for an unpaid debt where this person is the debtor and amount matches
    row = conn.execute(
        "SELECT id, creditor, debtor, amount, description FROM debts "
        "WHERE debtor=? AND paid=0 AND ABS(amount - ?) < 0.01 "
        "ORDER BY created_at ASC LIMIT 1",
        (sender_name.lower(), amount),
    ).fetchone()
    if row:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE debts SET paid=1, paid_at=? WHERE id=?", (now, row["id"]))
        conn.commit()
        debt = dict(row)
        conn.close()
        log.info(f"E-transfer auto-settled debt #{debt['id']}: {debt['debtor']} -> {debt['creditor']} ${debt['amount']:.2f}")
        return debt
    conn.close()
    return None


def get_due_reminders():
    """Get debts that need a reminder sent right now."""
    conn = _get_conn()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT id, creditor, debtor, amount, description, reminder_count, created_at "
        "FROM debts WHERE paid=0 AND next_reminder_at <= ?",
        (now,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def advance_reminder(debt_id):
    """Mark a reminder as sent and schedule the next one."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT reminder_count, created_at FROM debts WHERE id=?",
        (debt_id,),
    ).fetchone()
    if not row:
        conn.close()
        return
    new_count = row["reminder_count"] + 1
    next_at = _calc_next_reminder(row["created_at"], new_count)
    conn.execute(
        "UPDATE debts SET reminder_count=?, last_reminded_at=datetime('now','localtime'), "
        "next_reminder_at=? WHERE id=?",
        (new_count, next_at.strftime("%Y-%m-%d %H:%M:%S"), debt_id),
    )
    conn.commit()
    conn.close()


def get_active_debts():
    """Get all unpaid debts."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, creditor, debtor, amount, description, created_at, reminder_count "
        "FROM debts WHERE paid=0 ORDER BY created_at",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_debts_for_user(name):
    """Get unpaid debts where user is creditor or debtor."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, creditor, debtor, amount, description, created_at "
        "FROM debts WHERE paid=0 AND (creditor=? OR debtor=?) ORDER BY created_at",
        (name.lower(), name.lower()),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_debt_summary():
    """Build a summary string of all active debts for AI context."""
    debts = get_active_debts()
    if not debts:
        return "No outstanding family debts."
    lines = ["Outstanding family debts:"]
    for d in debts:
        creditor_nick = config.FAMILY_MEMBERS.get(d["creditor"], {}).get("nickname", d["creditor"])
        debtor_nick = config.FAMILY_MEMBERS.get(d["debtor"], {}).get("nickname", d["debtor"])
        desc = f" ({d['description']})" if d.get("description") else ""
        lines.append(f"- {debtor_nick} owes {creditor_nick} ${d['amount']:.2f}{desc}")
    return "\n".join(lines)
