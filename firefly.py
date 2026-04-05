"""Firefly III integration for household expense tracking."""
import json
import httpx
from datetime import datetime, date

import config

FIREFLY_BASE = config.FIREFLY_BASE
FIREFLY_TOKEN = config.FIREFLY_TOKEN
ACCOUNTS = config.FIREFLY_ACCOUNTS

LIABILITIES = {
    "rbc_mortgage": 9,
    "rbc_credit_line": 12,
    "rbc_mastercard": 18,
    "tangerine_mastercard": 21,
}

# Map keywords from bank statements to Firefly account IDs and types
STATEMENT_ACCOUNT_MAP = [
    # (keywords to match in account name/text, firefly_id, account_type)
    (["rbc", "no limit banking", "5064282"], 1, "asset"),
    (["tangerine", "chequing"], 3, "asset"),
    (["eq bank"], 5, "asset"),
    (["mastercard", "3314"], 18, "liability"),
    (["rbc", "mastercard"], 18, "liability"),
    (["tangerine", "mastercard"], 21, "liability"),
    (["rbc", "credit line"], 12, "liability"),
    (["line of credit"], 12, "liability"),
    (["rbc", "mortgage"], 9, "liability"),
]


def detect_account(*text_sources):
    """Detect Firefly account from statement text, filename, etc. Returns (account_id, account_type).
    Pass multiple strings — all will be searched."""
    lower = " ".join(str(s) for s in text_sources).lower()
    # Credit card / liability detection first (more specific)
    for keywords, acct_id, acct_type in STATEMENT_ACCOUNT_MAP:
        if acct_type == "liability" and all(k in lower for k in keywords):
            return acct_id, acct_type
    # Then asset accounts
    for keywords, acct_id, acct_type in STATEMENT_ACCOUNT_MAP:
        if acct_type == "asset" and any(k in lower for k in keywords):
            return acct_id, acct_type
    return 1, "asset"  # default to RBC Savings

CATEGORIES = [
    "Mortgage", "Groceries", "Electricity (Hydro One)", "Gas & Heating",
    "Water & Sewer", "Insurance - Home", "Insurance - Auto",
    "Insurance - Life & Health", "Fuel", "Internet", "Phone & Cell",
    "Subscriptions", "Medical & Pharmacy", "Vehicle Maintenance",
]

STORE_CATEGORY_MAP = {
    "fortinos": "Groceries",
    "lococo": "Groceries",
    "sobeys": "Groceries",
    "no frills": "Groceries",
    "walmart": "Groceries",
    "costco": "Groceries",
    "ruffin": "Pet Food & Supplies",
    "shell": "Fuel",
    "petro": "Fuel",
    "esso": "Fuel",
}


def _headers():
    return {
        "Authorization": f"Bearer {FIREFLY_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def log_transaction(description, amount, category="Groceries",
                    source_id=1, destination_name=None, tx_date=None,
                    tx_type="withdrawal", account_type="asset"):
    """Log a transaction to Firefly III."""
    if tx_date is None:
        tx_date = date.today().isoformat()
    if destination_name is None:
        destination_name = description

    tx = {
        "type": tx_type,
        "date": tx_date,
        "amount": str(abs(amount)),
        "description": description,
        "currency_code": "CAD",
        "category_name": category,
    }

    if account_type == "liability":
        # Credit card: withdrawals = purchases charged to card
        # deposits = payments to card
        if tx_type == "withdrawal":
            tx["source_id"] = str(source_id)
            tx["destination_name"] = destination_name
        else:
            tx["source_name"] = destination_name
            tx["destination_id"] = str(source_id)
    else:
        # Bank account: standard asset handling
        if tx_type == "deposit":
            tx["destination_id"] = str(source_id)
            tx["source_name"] = destination_name
        else:
            tx["source_id"] = str(source_id)
            tx["destination_name"] = destination_name

    payload = {"transactions": [tx]}

    resp = httpx.post(
        f"{FIREFLY_BASE}/transactions",
        headers=_headers(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["data"]["id"]


def log_receipt(store, total, items=None, tx_date=None):
    """Log a grocery receipt to Firefly III."""
    category = "Groceries"
    for key, cat in STORE_CATEGORY_MAP.items():
        if key in store.lower():
            category = cat
            break

    return log_transaction(
        description=f"{store} grocery run",
        amount=total,
        category=category,
        destination_name=store,
        tx_date=tx_date,
    )


def get_monthly_summary(year=None, month=None):
    """Get spending summary for a month from Firefly III."""
    if year is None:
        year = datetime.now().year
    if month is None:
        month = datetime.now().month

    start = f"{year}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1}-01-01"
    else:
        end = f"{year}-{month + 1:02d}-01"

    resp = httpx.get(
        f"{FIREFLY_BASE}/search/transactions",
        params={"query": f"date_after:{start} date_before:{end}", "limit": 100},
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    by_category = {}
    total = 0
    for entry in data.get("data", []):
        for tx in entry.get("attributes", {}).get("transactions", []):
            if tx.get("type") == "withdrawal":
                amt = float(tx.get("amount", 0))
                cat = tx.get("category_name", "Uncategorized")
                by_category[cat] = by_category.get(cat, 0) + amt
                total += amt

    return {"total": total, "by_category": by_category}


def get_recent_transactions(limit=10):
    """Get recent transactions."""
    resp = httpx.get(
        f"{FIREFLY_BASE}/transactions",
        params={"limit": limit, "type": "withdrawal"},
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    for entry in data.get("data", []):
        for tx in entry.get("attributes", {}).get("transactions", []):
            results.append({
                "date": tx.get("date", "")[:10],
                "description": tx.get("description", ""),
                "amount": tx.get("amount", "0"),
                "category": tx.get("category_name", ""),
            })
    return results


def get_account_balances():
    """Get current balances for all asset accounts."""
    resp = httpx.get(
        f"{FIREFLY_BASE}/accounts",
        params={"limit": 50, "type": "asset"},
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    balances = []
    for acct in data.get("data", []):
        attrs = acct.get("attributes", {})
        balances.append({
            "name": attrs.get("name", ""),
            "balance": attrs.get("current_balance", "0"),
            "currency": attrs.get("currency_code", "CAD"),
        })
    return balances
