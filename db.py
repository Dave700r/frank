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
    """Add item to shopping list. Returns (added: bool, existing_name: str|None).
    If a similar item is already on the list, returns (False, existing_name)."""
    conn = get_inventory_conn()
    # Check for existing similar item (case-insensitive fuzzy match)
    existing = conn.execute(
        "SELECT name FROM shopping_list WHERE bought=0 AND LOWER(name) LIKE ?",
        (f"%{name.lower()}%",),
    ).fetchone()
    if existing:
        conn.close()
        return False, existing["name"]
    conn.execute(
        "INSERT INTO shopping_list (name, category, qty, requested_by) VALUES (?, ?, ?, ?)",
        (name, category, qty, requested_by),
    )
    conn.commit()
    conn.close()
    return True, None


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


# --- Meal Plans ---

def _ensure_meal_plans_table():
    """Create meal_plans table if it doesn't exist."""
    conn = get_inventory_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meal_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            meal TEXT NOT NULL,
            recipe_id INTEGER,
            ingredients TEXT,
            planned_by TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()
    conn.close()

_ensure_meal_plans_table()


def add_meal_plan(date, meal, recipe_id=None, ingredients=None, planned_by=None):
    """Add a planned dinner. ingredients is a JSON list of ingredient strings."""
    import json as _json
    conn = get_inventory_conn()
    conn.execute(
        "INSERT INTO meal_plans (date, meal, recipe_id, ingredients, planned_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (date, meal, recipe_id, _json.dumps(ingredients) if ingredients else None, planned_by),
    )
    conn.commit()
    conn.close()


def get_meal_plans(upcoming_only=True):
    """Get meal plans. If upcoming_only, returns today and future only."""
    conn = get_inventory_conn()
    if upcoming_only:
        rows = conn.execute(
            "SELECT * FROM meal_plans WHERE date >= date('now','localtime') ORDER BY date",
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM meal_plans ORDER BY date DESC",
        ).fetchall()
    conn.close()
    return rows


def get_meal_plan_ingredients(upcoming_only=True):
    """Get all ingredients from upcoming meal plans, grouped by meal."""
    import json as _json
    plans = get_meal_plans(upcoming_only=upcoming_only)
    result = []
    for plan in plans:
        ingredients = []
        if plan["ingredients"]:
            try:
                ingredients = _json.loads(plan["ingredients"])
            except (ValueError, TypeError):
                pass
        result.append({
            "id": plan["id"],
            "date": plan["date"],
            "meal": plan["meal"],
            "recipe_id": plan["recipe_id"],
            "ingredients": ingredients,
        })
    return result


def remove_meal_plan(meal_name=None, date=None, plan_id=None):
    """Remove a meal plan by name, date, or id. Returns number removed."""
    conn = get_inventory_conn()
    if plan_id:
        cur = conn.execute("DELETE FROM meal_plans WHERE id=?", (plan_id,))
    elif date:
        cur = conn.execute("DELETE FROM meal_plans WHERE date=?", (date,))
    elif meal_name:
        cur = conn.execute(
            "DELETE FROM meal_plans WHERE LOWER(meal) LIKE ?",
            (f"%{meal_name.lower()}%",),
        )
    else:
        conn.close()
        return 0
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected


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
