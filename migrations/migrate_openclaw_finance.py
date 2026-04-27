"""One-time migration of legacy OpenClaw finance data into Frank's per-user schema.

Source: /home/dave/.openclaw/workspace/finance/finance.db (read-only)
  - accounts(id, name, institution, type, account_number, currency,
             current_balance, last_updated)
  - transactions(id, account_id, date, description, amount, balance,
                 category, subcategory, notes, source, source_file,
                 receipt_id, imported_at) where amount is signed.

Target: /home/dave/family-bot/finance.db (Frank's schema, see finance.py)
  - accounts(id, user_name, name, type, balance, created_at)
  - transactions(id, user_name, date, amount, description, category,
                 account_id, tx_type, created_at) where amount is positive
                 and tx_type is 'deposit' or 'withdrawal'.

All migrated rows are attributed to user_name='dave' since the legacy DB
predates Frank's multi-user model and only ever held Dave's transactions.
Account IDs are preserved so transactions.account_id FKs stay aligned.

Idempotency: uses INSERT OR IGNORE on the UNIQUE(user_name, name) account
key so re-running the script will not duplicate accounts. Transactions
have no natural unique key — the script aborts if Frank's transactions
table is non-empty, to avoid duplicate inserts.

Run:
    /home/dave/family-bot-env/bin/python3 \\
        /home/dave/family-bot/migrations/migrate_openclaw_finance.py
"""
import sqlite3
import sys
from pathlib import Path

SOURCE_DB = "/home/dave/.openclaw/workspace/finance/finance.db"
TARGET_DB = "/home/dave/family-bot/finance.db"
TARGET_USER = "dave"


def main():
    source = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import finance
    finance._init_tables(sqlite3.connect(TARGET_DB))

    target = sqlite3.connect(TARGET_DB)
    target.execute("PRAGMA foreign_keys=OFF")

    existing_tx = target.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    if existing_tx > 0:
        print(f"ABORT: target transactions table already has {existing_tx} rows. "
              "Refusing to migrate to avoid duplicates.", file=sys.stderr)
        return 1

    accounts_inserted = 0
    for row in source.execute(
        "SELECT id, name, type, current_balance FROM accounts"
    ):
        cur = target.execute(
            "INSERT OR IGNORE INTO accounts (id, user_name, name, type, balance) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["id"], TARGET_USER, row["name"], row["type"] or "chequing",
             row["current_balance"] or 0),
        )
        accounts_inserted += cur.rowcount
    target.commit()
    print(f"Accounts inserted: {accounts_inserted}")

    tx_inserted = 0
    for row in source.execute(
        "SELECT id, account_id, date, amount, description, category "
        "FROM transactions ORDER BY id"
    ):
        amount = row["amount"] or 0
        tx_type = "deposit" if amount > 0 else "withdrawal"
        target.execute(
            "INSERT INTO transactions (id, user_name, date, amount, description, "
            "category, account_id, tx_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], TARGET_USER, row["date"], abs(amount),
             row["description"] or "", row["category"] or "Uncategorized",
             row["account_id"], tx_type),
        )
        tx_inserted += 1
    target.commit()
    print(f"Transactions inserted: {tx_inserted}")

    sample = target.execute(
        "SELECT date, description, amount, tx_type, category FROM transactions "
        "ORDER BY date DESC LIMIT 5"
    ).fetchall()
    print("Sample (most recent 5):")
    for r in sample:
        print(" ", tuple(r))

    source.close()
    target.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
