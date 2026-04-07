"""Built-in finance tracking — lightweight alternative to Firefly III.
Per-user accounts and transactions in SQLite. No external service needed."""
import sqlite3
import logging
from datetime import datetime, date
from pathlib import Path

import config

log = logging.getLogger("family-bot.finance")

DB_PATH = config.FINANCE_DB

STORE_CATEGORY_MAP = {
    "fortinos": "Groceries", "lococo": "Groceries", "sobeys": "Groceries",
    "no frills": "Groceries", "walmart": "Groceries", "costco": "Groceries",
    "food basics": "Groceries", "metro": "Groceries", "freshco": "Groceries",
    "ruffin": "Pet Supplies", "petsmart": "Pet Supplies",
    "shell": "Fuel", "petro": "Fuel", "esso": "Fuel", "pioneer": "Fuel",
    "canadian tire": "Auto & Home", "home depot": "Home Improvement",
    "shoppers": "Pharmacy", "rexall": "Pharmacy",
    "amazon": "Online Shopping", "dollarama": "Household",
}


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _init_tables(conn)
    return conn


def _init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name TEXT NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'chequing',
            balance REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(user_name, name)
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name TEXT NOT NULL,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            category TEXT DEFAULT 'Uncategorized',
            account_id INTEGER,
            tx_type TEXT DEFAULT 'withdrawal',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_tx_user_date ON transactions(user_name, date);
    """)
    conn.commit()


# --- Accounts ---

def add_account(user_name, name, acct_type="chequing", balance=0):
    """Add a financial account for a user."""
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (user_name, name, type, balance) VALUES (?, ?, ?, ?)",
            (user_name, name, acct_type, balance)
        )
        conn.commit()
        log.info(f"Account added for {user_name}: {name} ({acct_type})")
        return True
    except Exception as e:
        log.error(f"Add account failed: {e}")
        return False
    finally:
        conn.close()


def get_accounts(user_name):
    """Get all accounts for a user."""
    conn = _conn()
    rows = conn.execute(
        "SELECT id, name, type, balance FROM accounts WHERE user_name=? ORDER BY name",
        (user_name,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_balance(account_id, amount, tx_type="withdrawal"):
    """Update account balance after a transaction."""
    conn = _conn()
    if tx_type == "deposit":
        conn.execute("UPDATE accounts SET balance = balance + ? WHERE id=?", (abs(amount), account_id))
    else:
        conn.execute("UPDATE accounts SET balance = balance - ? WHERE id=?", (abs(amount), account_id))
    conn.commit()
    conn.close()


# --- Transactions ---

def log_transaction(user_name, description, amount, category="Uncategorized",
                    account_id=None, tx_type="withdrawal", tx_date=None):
    """Log a financial transaction."""
    if tx_date is None:
        tx_date = date.today().isoformat()

    # Auto-detect category from store name
    if category == "Uncategorized":
        for key, cat in STORE_CATEGORY_MAP.items():
            if key in description.lower():
                category = cat
                break

    conn = _conn()
    conn.execute(
        "INSERT INTO transactions (user_name, date, amount, description, category, account_id, tx_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_name, tx_date, abs(amount), description, category, account_id, tx_type)
    )
    conn.commit()
    conn.close()

    # Update account balance if linked
    if account_id:
        update_balance(account_id, amount, tx_type)

    log.info(f"Transaction logged for {user_name}: {tx_type} ${abs(amount):.2f} — {description} [{category}]")
    return True


def log_receipt(user_name, store, total, items=None, tx_date=None):
    """Log a receipt/purchase."""
    category = "Groceries"
    for key, cat in STORE_CATEGORY_MAP.items():
        if key in store.lower():
            category = cat
            break
    return log_transaction(user_name, store, total, category=category, tx_date=tx_date)


def get_recent(user_name, limit=10):
    """Get recent transactions for a user."""
    conn = _conn()
    rows = conn.execute(
        "SELECT id, date, amount, description, category, tx_type "
        "FROM transactions WHERE user_name=? ORDER BY date DESC, id DESC LIMIT ?",
        (user_name, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_monthly_summary(user_name, year=None, month=None):
    """Get spending summary for a user for a given month."""
    if year is None:
        year = datetime.now().year
    if month is None:
        month = datetime.now().month

    start = f"{year}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1}-01-01"
    else:
        end = f"{year}-{month + 1:02d}-01"

    conn = _conn()

    # Total spending
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM transactions "
        "WHERE user_name=? AND date >= ? AND date < ? AND tx_type='withdrawal'",
        (user_name, start, end)
    ).fetchone()
    total = row["total"] if row else 0

    # Total income
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM transactions "
        "WHERE user_name=? AND date >= ? AND date < ? AND tx_type='deposit'",
        (user_name, start, end)
    ).fetchone()
    income = row["total"] if row else 0

    # By category
    rows = conn.execute(
        "SELECT category, COALESCE(SUM(amount), 0) as total "
        "FROM transactions WHERE user_name=? AND date >= ? AND date < ? AND tx_type='withdrawal' "
        "GROUP BY category ORDER BY total DESC",
        (user_name, start, end)
    ).fetchall()
    by_category = {r["category"]: r["total"] for r in rows}

    # Transaction count
    count = conn.execute(
        "SELECT COUNT(*) as n FROM transactions WHERE user_name=? AND date >= ? AND date < ?",
        (user_name, start, end)
    ).fetchone()["n"]

    conn.close()

    return {
        "total_spent": total,
        "total_income": income,
        "by_category": by_category,
        "transaction_count": count,
        "month": month,
        "year": year,
    }


def search_transactions(user_name, query, limit=10):
    """Search transactions by description or category."""
    conn = _conn()
    rows = conn.execute(
        "SELECT id, date, amount, description, category, tx_type "
        "FROM transactions WHERE user_name=? AND (description LIKE ? OR category LIKE ?) "
        "ORDER BY date DESC LIMIT ?",
        (user_name, f"%{query}%", f"%{query}%", limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_spending_by_store(user_name, months=1):
    """Get spending grouped by store/description for the last N months."""
    conn = _conn()
    rows = conn.execute(
        "SELECT description, SUM(amount) as total, COUNT(*) as count "
        "FROM transactions WHERE user_name=? AND tx_type='withdrawal' "
        "AND date >= date('now', ? || ' months') "
        "GROUP BY description ORDER BY total DESC LIMIT 15",
        (user_name, f"-{months}")
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Household summary (shared view, no private details) ---

def get_household_summary(month=None, year=None):
    """Get combined household spending — totals only, no individual breakdowns."""
    if year is None:
        year = datetime.now().year
    if month is None:
        month = datetime.now().month

    start = f"{year}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1}-01-01"
    else:
        end = f"{year}-{month + 1:02d}-01"

    conn = _conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM transactions "
        "WHERE date >= ? AND date < ? AND tx_type='withdrawal'",
        (start, end)
    ).fetchone()

    rows = conn.execute(
        "SELECT category, COALESCE(SUM(amount), 0) as total "
        "FROM transactions WHERE date >= ? AND date < ? AND tx_type='withdrawal' "
        "GROUP BY category ORDER BY total DESC",
        (start, end)
    ).fetchall()

    conn.close()
    return {
        "total": row["total"] if row else 0,
        "by_category": {r["category"]: r["total"] for r in rows},
    }
