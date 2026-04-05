"""Database access layer for inventory and finance."""
import sqlite3
from datetime import datetime, date
from config import INVENTORY_DB, FINANCE_DB


def get_inventory_conn():
    conn = sqlite3.connect(INVENTORY_DB)
    conn.row_factory = sqlite3.Row
    return conn


def get_finance_conn():
    conn = sqlite3.connect(FINANCE_DB)
    conn.row_factory = sqlite3.Row
    return conn


# --- Shopping List ---

def get_shopping_list():
    """Get all unbought items from the shopping list."""
    conn = get_inventory_conn()
    rows = conn.execute(
        "SELECT name, category, qty, requested_by, added_date "
        "FROM shopping_list WHERE bought=0 ORDER BY category, name"
    ).fetchall()
    conn.close()
    return rows


def add_shopping_item(name, category="other", qty=None, requested_by=None):
    conn = get_inventory_conn()
    conn.execute(
        "INSERT INTO shopping_list (name, category, qty, requested_by) VALUES (?, ?, ?, ?)",
        (name, category, qty, requested_by),
    )
    conn.commit()
    conn.close()


def remove_shopping_item(name):
    """Remove an item from the shopping list. Returns number of items removed."""
    conn = get_inventory_conn()
    cur = conn.execute(
        "DELETE FROM shopping_list WHERE bought=0 AND LOWER(name) LIKE ?",
        (f"%{name.lower()}%",),
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected


def mark_item_bought(name, bought_by=None):
    """Mark a shopping list item as bought. Returns True if found."""
    conn = get_inventory_conn()
    cur = conn.execute(
        "UPDATE shopping_list SET bought=1, bought_date=?, bought_by=? "
        "WHERE bought=0 AND LOWER(name) LIKE ?",
        (date.today().isoformat(), bought_by, f"%{name.lower()}%"),
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


# --- Inventory ---

def get_inventory():
    """Get all active inventory items."""
    conn = get_inventory_conn()
    rows = conn.execute(
        "SELECT name, unit, category, current_qty FROM items "
        "WHERE active=1 ORDER BY category, name"
    ).fetchall()
    conn.close()
    return rows


def get_low_stock_items():
    """Items that are out of stock or very low."""
    conn = get_inventory_conn()
    rows = conn.execute(
        "SELECT name, unit, category, current_qty FROM items "
        "WHERE active=1 AND (current_qty=0 OR current_qty IS NULL) "
        "ORDER BY category, name"
    ).fetchall()
    conn.close()
    return rows


def update_item_qty(name, qty):
    """Update current quantity of an inventory item."""
    conn = get_inventory_conn()
    cur = conn.execute(
        "UPDATE items SET current_qty=? WHERE LOWER(name) LIKE ? AND active=1",
        (qty, f"%{name.lower()}%"),
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def record_event(item_name, event_type, qty=None, note=None):
    """Record a bought/used/out event."""
    conn = get_inventory_conn()
    item = conn.execute(
        "SELECT id FROM items WHERE LOWER(name) LIKE ? AND active=1",
        (f"%{item_name.lower()}%",),
    ).fetchone()
    if item:
        conn.execute(
            "INSERT INTO events (item_id, event_type, qty, note) VALUES (?, ?, ?, ?)",
            (item["id"], event_type, qty, note),
        )
        conn.commit()
    conn.close()
    return item is not None


# --- Consumption Rates ---

def get_consumption_alerts():
    """Items approaching reorder based on consumption rates."""
    conn = get_inventory_conn()
    rows = conn.execute(
        """SELECT i.name, i.current_qty, i.unit, cr.days_per_unit,
                  i.last_purchased,
                  JULIANDAY('now','localtime') - JULIANDAY(i.last_purchased) as days_since
           FROM consumption_rates cr
           JOIN items i ON i.id = cr.item_id
           WHERE i.active=1 AND i.last_purchased IS NOT NULL
           AND (JULIANDAY('now','localtime') - JULIANDAY(i.last_purchased)) > cr.days_per_unit * 0.8
        """
    ).fetchall()
    conn.close()
    return rows


# --- Finance ---

def get_monthly_spend(year=None, month=None):
    """Get total spending for a given month."""
    if year is None:
        year = datetime.now().year
    if month is None:
        month = datetime.now().month
    start = f"{year}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1}-01-01"
    else:
        end = f"{year}-{month + 1:02d}-01"

    conn = get_finance_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(ABS(amount)), 0) as total FROM transactions "
        "WHERE date >= ? AND date < ? AND amount < 0",
        (start, end),
    ).fetchone()
    conn.close()
    return row["total"] if row else 0


def get_spend_by_category(year=None, month=None):
    """Spending breakdown by category for a month."""
    if year is None:
        year = datetime.now().year
    if month is None:
        month = datetime.now().month
    start = f"{year}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1}-01-01"
    else:
        end = f"{year}-{month + 1:02d}-01"

    conn = get_finance_conn()
    rows = conn.execute(
        "SELECT category, COALESCE(SUM(ABS(amount)), 0) as total "
        "FROM transactions WHERE date >= ? AND date < ? AND amount < 0 "
        "GROUP BY category ORDER BY total DESC",
        (start, end),
    ).fetchall()
    conn.close()
    return rows


def log_spend(store, total, items=None):
    """Log a spend to the spend-log.json file."""
    import json
    from config import SPEND_LOG

    entry = {
        "store": store,
        "date": date.today().isoformat(),
        "total": total,
        "items": items or [],
        "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    data = []
    if SPEND_LOG.exists():
        with open(SPEND_LOG) as f:
            data = json.load(f)

    data.append(entry)
    with open(SPEND_LOG, "w") as f:
        json.dump(data, f, indent=2)

    return entry
