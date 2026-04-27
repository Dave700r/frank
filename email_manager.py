"""Automated daily email management for Frank.

Scans Proton inbox, classifies emails, processes bank statements and
e-transfers to Firefly, auto-confirms family payments, deletes junk,
and reports to Dave.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import config
import email_client
import firefly
import ai

log = logging.getLogger("family-bot.email-manager")

STATE_FILE = config.WORKSPACE / "email_manager_state.json"

# ─── Sender Rules (hardcoded, deterministic) ─────────────────────────────────

SENDER_RULES = {
    # Interac e-transfers
    "catch@payments.interac.ca": "etransfer",
    "notify@payments.interac.ca": "etransfer",

    # Bank statements / notifications
    "rbcroyalbank@service.rbc.com": "bank_notification",
    "alert@eqbank.ca": "bank_notification",
    "donotreply@tangerine.ca": "bank_notification",

    # Bills
    "noreply@ebill.hydroone.com": "bill",
    "DONOTREPLY@rci.rogers.com": "bill",
    "notifications@rci.rogers.com": "bill",

    # Known junk / marketing
    "rbcroyalbank@offers.rbc.com": "junk",
    "reply@marketing.eqbank.ca": "junk",
    "forwardbanking@email.tangerine.ca": "junk",
    "forwardbanking@e.tangerine.ca": "junk",
    "rbcrewards@newsletters.rbc.com": "junk",
    "no-reply@customervoice360.rbc.com": "junk",

    # Camera alerts (UniFi)
    "no-reply@notifications.ui.com": "junk",
}

SUBJECT_RULES = [
    (r"(?i)e?-?statement.*(ready|available|prepared|is ready)", "bank_statement"),
    (r"(?i)your.*statement is ready", "bank_statement"),
    (r"(?i)interac.*e-?transfer", "etransfer"),
    (r"(?i)e-?transfer.*\$[\d,]+", "etransfer"),
    (r"(?i)(auto-?deposit|autodeposit)", "etransfer"),
    (r"(?i)transfer.*has been.*deposited", "etransfer"),
    (r"(?i)transfer.*accepted", "etransfer"),
    (r"(?i)your.*(monthly\s+)?bill", "bill"),
    (r"(?i)payment\s+(due|reminder|confirmation)", "bill"),
    (r"(?i)(security\s+alert|suspicious|password\s+change|login\s+attempt|new\s+device)", "security"),
    (r"(?i)(2-?step|two-?factor|verification\s+code|one-?time\s+code)", "security"),
    (r"(?i)(unsubscribe|promo|special\s+offer|limited\s+time|% off|sale\s+ends)", "junk"),
    (r"(?i)has\s+recorded\s+(a\s+)?(motion|person|vehicle|animal)", "junk"),
]

# E-transfer extraction patterns
ETRANSFER_PATTERNS = [
    # "Your $28.92 transfer to EMILY ST AUBIN has been successfully deposited."
    (r"(?i)your\s+\$?([\d,]+\.?\d*)\s+transfer\s+to\s+(.+?)\s+has\s+been\s+(successfully\s+)?deposited",
     "sent_deposited"),
    # "Your funds $5,000.00 to ST. AUBIN MORRISON has been successfully deposited"
    (r"(?i)your\s+funds?\s+\$?([\d,]+\.?\d*)\s+to\s+(.+?)\s+has\s+been\s+(successfully\s+)?deposited",
     "sent_deposited"),
    # "Colin has accepted your transfer of $153.98"
    # Strip "Interac e-Transfer: " prefix from subject before matching
    (r"(?i)^(?:Interac\s+e-?Transfer:\s*)?(.+?)\s+has\s+accepted\s+your\s+transfer\s+of\s+\$?([\d,]+\.?\d*)",
     "sent_accepted"),
    # "Claim your $300.00 from DAVID ST-AUBIN"
    (r"(?i)claim\s+your\s+\$?([\d,]+\.?\d*)\s+from\s+(.+?)(?:\s+by\s+|\.|$)",
     "received"),
    # "You received $X from Y" / "deposit of $X from Y"
    (r"(?i)(?:received|deposit\s+of)\s+\$?([\d,]+\.?\d*)\s+from\s+(.+?)(?:\.|$)",
     "received"),
    # "DAVID ST-AUBIN sent you $X"
    (r"(?i)(.+?)\s+sent\s+you\s+\$?([\d,]+\.?\d*)",
     "received"),
]

# Bill payee to Firefly category mapping
BILL_CATEGORY_MAP = {
    "hydro": "Electricity (Hydro One)",
    "enbridge": "Gas & Heating",
    "union gas": "Gas & Heating",
    "rogers": "Internet",
    "bell": "Phone & Cell",
    "insurance": "Insurance - Home",
    "mortgage": "Mortgage",
    "water": "Water & Sewer",
}


# ─── State Management ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "processed_message_ids": {},
            "sender_classifications": {},
            "last_scan": None,
        }


def _atomic_write_json(path: Path, data) -> None:
    """Write JSON atomically via tmp + os.replace so a mid-write crash can't corrupt the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _save_state(state: dict):
    _atomic_write_json(STATE_FILE, state)


def _prune_processed_ids(state: dict):
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()
    state["processed_message_ids"] = {
        k: v for k, v in state["processed_message_ids"].items()
        if v > cutoff
    }


def _prune_learned(state: dict):
    cutoff = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    state["sender_classifications"] = {
        k: v for k, v in state["sender_classifications"].items()
        if v.get("source") == "manual" or v.get("hit_count", 0) >= 3 or v.get("last_updated", "") > cutoff
    }


# ─── Classification ───────────────────────────────────────────────────────────

def classify(from_addr: str, subject: str, body_preview: str, state: dict) -> str:
    """Classify an email. Returns category string."""
    addr_lower = from_addr.lower()

    # 1. Hardcoded sender rules
    for sender, cat in SENDER_RULES.items():
        if sender.lower() == addr_lower:
            _update_learned(state, addr_lower, cat, "rule")
            return cat

    # 2. Subject regex rules (override sender if strong match)
    for pattern, cat in SUBJECT_RULES:
        if re.search(pattern, subject):
            _update_learned(state, addr_lower, cat, "rule")
            return cat

    # 3. Learned classifications
    learned = state.get("sender_classifications", {}).get(addr_lower, {})
    if learned.get("confidence", 0) >= 0.7:
        learned["hit_count"] = learned.get("hit_count", 0) + 1
        if learned["hit_count"] >= 3:
            learned["confidence"] = 1.0
        learned["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        return learned["category"]

    # 4. AI fallback (rate-limited: max 10 per scan to avoid hammering API)
    ai_calls = state.get("_ai_calls_this_scan", 0)
    if ai_calls < 10:
        state["_ai_calls_this_scan"] = ai_calls + 1
        result = ai.classify_email(from_addr, subject, body_preview)
        _update_learned(state, addr_lower, result["category"], "ai", result["confidence"])
        return result["category"]
    else:
        # Too many AI calls, default to "important" (safe fallback — won't delete)
        _update_learned(state, addr_lower, "important", "default", 0.3)
        return "important"


def _update_learned(state: dict, addr: str, category: str, source: str, confidence: float = 1.0):
    classifications = state.setdefault("sender_classifications", {})
    existing = classifications.get(addr, {})
    hit_count = existing.get("hit_count", 0) + 1
    classifications[addr] = {
        "category": category,
        "confidence": confidence if source != "rule" else 1.0,
        "source": source,
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "hit_count": hit_count,
    }


# ─── Processing Pipelines ────────────────────────────────────────────────────

def process_etransfer_email(email_data: dict) -> dict:
    """Parse Interac e-transfer email and log to Firefly."""
    subject = email_data.get("subject", "")
    # Strip the common "Interac e-Transfer: " prefix for cleaner matching
    clean_subject = re.sub(r'^Interac\s+e-?Transfer:\s*', '', subject, flags=re.IGNORECASE)
    body = email_data.get("body_preview", "") or email_data.get("body", "")
    text = f"{clean_subject} {body}"

    result = {
        "type": None,
        "amount": None,
        "counterparty": None,
        "logged_to_firefly": False,
        "matched_payment": None,
    }

    for pattern, transfer_type in ETRANSFER_PATTERNS:
        match = re.search(pattern, text)
        if match:
            groups = match.groups()
            if transfer_type in ("sent_deposited", "sent_accepted"):
                if transfer_type == "sent_deposited":
                    result["amount"] = float(groups[0].replace(",", ""))
                    result["counterparty"] = groups[1].strip()
                else:
                    result["counterparty"] = groups[0].strip()
                    result["amount"] = float(groups[1].replace(",", ""))
                result["type"] = "sent"
            elif transfer_type == "received":
                if groups[0].replace(",", "").replace(".", "").isdigit():
                    result["amount"] = float(groups[0].replace(",", ""))
                    result["counterparty"] = groups[1].strip()
                else:
                    result["counterparty"] = groups[0].strip()
                    result["amount"] = float(groups[1].replace(",", ""))
                result["type"] = "received"
            break

    if not result["amount"]:
        # Last resort: just find any dollar amount
        amt_match = re.search(r'\$\s*([\d,]+\.?\d*)', text)
        if amt_match:
            result["amount"] = float(amt_match.group(1).replace(",", ""))

    if result["amount"] and result["amount"] > 0:
        try:
            if result["type"] == "sent":
                description = f"Interac e-Transfer to {result['counterparty'] or 'unknown'}"
                firefly.log_transaction(
                    description=description,
                    amount=result["amount"],
                    category="Transfer",
                    source_id=1,  # RBC Savings (most e-transfers come from here)
                    tx_type="withdrawal",
                )
            elif result["type"] == "received":
                description = f"Interac e-Transfer from {result['counterparty'] or 'unknown'}"
                firefly.log_transaction(
                    description=description,
                    amount=result["amount"],
                    category="Transfer",
                    source_id=1,
                    tx_type="deposit",
                )
            result["logged_to_firefly"] = True
        except Exception as e:
            log.error(f"Firefly e-transfer log failed: {e}")

        # Try to match payment trackers
        if result["type"] == "sent" and result["counterparty"]:
            matched = _match_payment_trackers(result["amount"], result["counterparty"])
            if matched:
                result["matched_payment"] = matched

    return result


def _match_payment_trackers(amount: float, counterparty: str) -> str:
    """Find and auto-confirm pending payment trackers matching this e-transfer."""
    for tracker_path in config.WORKSPACE.glob("*_payment_tracker.json"):
        try:
            with open(tracker_path) as f:
                data = json.load(f)
            if data.get("status") != "pending":
                continue
            if abs(float(data.get("amount", 0)) - amount) < 0.01:
                creditor = data.get("creditor", "").lower()
                cp_lower = counterparty.lower()
                if creditor in cp_lower or cp_lower in creditor:
                    data["status"] = "paid"
                    data["paid"] = True
                    data["reminders_active"] = False
                    data["confirmed_by"] = "email_manager"
                    data["confirmed_at"] = datetime.now().isoformat()
                    data["payment_method"] = "Interac e-Transfer (auto-detected)"
                    _atomic_write_json(Path(tracker_path), data)
                    desc = f"{data.get('creditor', '?')} ${amount:.2f} ({data.get('purpose', '?')})"
                    log.info(f"Auto-confirmed payment: {desc}")
                    return desc
        except Exception as e:
            log.warning(f"Payment tracker match error: {e}")
    return None


def process_bill_email(email_data: dict) -> dict:
    """Parse a bill email and log to Firefly."""
    subject = email_data.get("subject", "")
    body = email_data.get("body_preview", "") or email_data.get("body", "")
    sender = email_data.get("from", "")

    parsed = email_client.parse_bill_email(subject, body, sender)
    result = {
        "payee": parsed["payee"] if parsed else subject,
        "amount": parsed["amount"] if parsed else None,
        "due_date": parsed.get("due_date") if parsed else None,
        "logged_to_firefly": False,
    }

    if result["amount"]:
        # Determine category from payee
        category = "Bills & Services"
        payee_lower = (result["payee"] + " " + sender).lower()
        for keyword, cat in BILL_CATEGORY_MAP.items():
            if keyword in payee_lower:
                category = cat
                break

        try:
            firefly.log_transaction(
                description=result["payee"],
                amount=result["amount"],
                category=category,
                tx_type="withdrawal",
            )
            result["logged_to_firefly"] = True
        except Exception as e:
            log.error(f"Firefly bill log failed: {e}")

    return result


def process_statement_email(email_data: dict) -> dict:
    """Download PDF attachment, parse bank statement, log to Firefly."""
    result = {
        "account": None,
        "period": None,
        "transaction_count": 0,
        "logged_to_firefly": False,
        "error": None,
    }

    msg_id = email_data.get("id")
    if not msg_id:
        result["error"] = "No message ID"
        return result

    try:
        # Download attachments
        paths = email_client.download_attachment(msg_id)
        if not paths:
            result["error"] = "No attachments found"
            return result

        # Find PDF
        pdf_path = None
        for p in paths if isinstance(paths, list) else [paths]:
            if str(p).lower().endswith(".pdf"):
                pdf_path = str(p)
                break

        if not pdf_path:
            result["error"] = "No PDF attachment"
            return result

        # Detect account from email metadata + PDF
        subject = email_data.get("subject", "")
        sender = email_data.get("from", "")
        account_id, account_type = firefly.detect_account(subject, sender)

        # Parse statement with AI
        parsed = ai.parse_bank_statement(pdf_path)
        if not parsed or not parsed.get("transactions"):
            result["error"] = "No transactions extracted"
            return result

        # Re-detect account from PDF text if available
        if parsed.get("_raw_text"):
            pdf_account_id, pdf_account_type = firefly.detect_account(parsed["_raw_text"])
            if pdf_account_id != 1:  # If PDF detection found something specific (not default)
                account_id, account_type = pdf_account_id, pdf_account_type

        result["account"] = parsed.get("account", f"Account {account_id}")
        result["period"] = parsed.get("period", "unknown")

        # Log each transaction
        logged = 0
        for tx in parsed["transactions"]:
            try:
                firefly.log_transaction(
                    description=tx.get("description", "Unknown"),
                    amount=float(tx.get("amount", 0)),
                    category=tx.get("category", "Uncategorized"),
                    source_id=account_id,
                    tx_date=tx.get("date"),
                    tx_type=tx.get("type", "withdrawal"),
                    account_type=account_type,
                )
                logged += 1
            except Exception as e:
                log.warning(f"Failed to log transaction: {e}")

        result["transaction_count"] = logged
        result["logged_to_firefly"] = logged > 0

        # Clean up PDF
        try:
            os.unlink(pdf_path)
        except Exception:
            pass

    except Exception as e:
        result["error"] = str(e)
        log.error(f"Statement processing failed: {e}", exc_info=True)

    return result


# ─── Main Scan ────────────────────────────────────────────────────────────────

def _sync_scan_and_process() -> dict:
    """Synchronous scan — runs in executor thread."""
    state = _load_state()
    state["_ai_calls_this_scan"] = 0  # Reset AI rate limiter
    report = {
        "date": datetime.now().strftime("%B %d, %Y"),
        "emails_scanned": 0,
        "statements_processed": [],
        "etransfers_processed": [],
        "bills_found": [],
        "payments_confirmed": [],
        "junk_deleted": 0,
        "security_flagged": [],
        "important_flagged": [],
        "errors": [],
    }

    try:
        # Fetch emails from last 2 days (overlap for safety)
        since = (datetime.now() - timedelta(days=2)).strftime("%d-%b-%Y")
        emails = email_client.search_by_date(since_date=since, limit=200)
        report["emails_scanned"] = len(emails)
        log.info(f"Email scan: {len(emails)} emails in last 2 days")
    except Exception as e:
        report["errors"].append(f"IMAP connection failed: {e}")
        log.error(f"Email scan IMAP failed: {e}")
        return report

    for em in emails:
        msg_header_id = em.get("message_id_header", "")

        # Skip already-processed
        if msg_header_id and msg_header_id in state["processed_message_ids"]:
            continue

        try:
            from_addr = em.get("from_addr", em.get("from", ""))
            subject = em.get("subject", "")
            preview = em.get("body_preview", "")

            category = classify(from_addr, subject, preview, state)
            log.debug(f"Classified '{subject[:50]}' from {from_addr} as {category}")

            if category == "bank_statement" and em.get("has_attachments"):
                result = process_statement_email(em)
                if result.get("error"):
                    report["errors"].append(f"Statement: {result['error']}")
                else:
                    report["statements_processed"].append(result)

            elif category == "etransfer":
                result = process_etransfer_email(em)
                if result.get("amount"):
                    report["etransfers_processed"].append(result)
                    if result.get("matched_payment"):
                        report["payments_confirmed"].append(result["matched_payment"])

            elif category == "bill":
                result = process_bill_email(em)
                if result.get("amount"):
                    report["bills_found"].append(result)

            elif category == "security":
                report["security_flagged"].append({
                    "from": em.get("from", ""),
                    "subject": subject,
                })

            elif category == "junk":
                try:
                    email_client.delete_email(em["id"])
                    report["junk_deleted"] += 1
                except Exception as e:
                    log.warning(f"Junk delete failed: {e}")

            # Mark as processed
            if msg_header_id:
                state["processed_message_ids"][msg_header_id] = datetime.now().isoformat()

        except Exception as e:
            report["errors"].append(f"{em.get('subject', '?')[:40]}: {e}")
            log.error(f"Email processing error: {e}", exc_info=True)

    # Prune old data
    _prune_processed_ids(state)
    _prune_learned(state)
    state["last_scan"] = datetime.now().isoformat()
    _save_state(state)

    return report


def format_daily_report(report: dict) -> str:
    """Format scan results as a human-readable report."""
    lines = [f"EMAIL SCAN REPORT — {report['date']}\n"]
    lines.append(f"Scanned: {report['emails_scanned']} emails\n")

    if report["statements_processed"]:
        lines.append("STATEMENTS PROCESSED:")
        for s in report["statements_processed"]:
            lines.append(f"  {s.get('account', '?')} — {s.get('period', '?')}: "
                         f"{s['transaction_count']} transactions logged")

    if report["etransfers_processed"]:
        lines.append("\nE-TRANSFERS:")
        for t in report["etransfers_processed"]:
            direction = "Sent to" if t.get("type") == "sent" else "Received from"
            lines.append(f"  {direction} {t.get('counterparty', '?')}: ${t.get('amount', 0):.2f}")

    if report["payments_confirmed"]:
        lines.append("\nPAYMENTS AUTO-CONFIRMED:")
        for p in report["payments_confirmed"]:
            lines.append(f"  {p}")

    if report["bills_found"]:
        lines.append("\nBILLS:")
        for b in report["bills_found"]:
            due = f" (due {b['due_date']})" if b.get("due_date") else ""
            lines.append(f"  {b.get('payee', '?')}: ${b.get('amount', 0):.2f}{due}")

    if report["security_flagged"]:
        lines.append("\n⚠ SECURITY ALERTS (review manually):")
        for s in report["security_flagged"]:
            lines.append(f"  {s['subject']}")

    if report["junk_deleted"]:
        lines.append(f"\nJunk deleted: {report['junk_deleted']}")

    if report["errors"]:
        lines.append(f"\nErrors ({len(report['errors'])}):")
        for e in report["errors"][:5]:
            lines.append(f"  {e}")

    if not any([report["statements_processed"], report["etransfers_processed"],
                report["bills_found"], report["junk_deleted"]]):
        lines.append("Nothing actionable found today.")

    return "\n".join(lines)


async def daily_email_scan(app) -> dict:
    """Main daily scan job. Called by APScheduler."""
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(None, _sync_scan_and_process)

    report_text = format_daily_report(report)
    log.info(f"Email scan complete: {report['emails_scanned']} scanned, "
             f"{len(report['etransfers_processed'])} transfers, "
             f"{report['junk_deleted']} junk deleted")

    # Send report to Dave
    dave_id = config.FAMILY_MEMBERS["dave"]["telegram_id"]
    try:
        await app.bot.send_message(chat_id=dave_id, text=report_text)
    except Exception as e:
        log.warning(f"Telegram report failed: {e}")

    try:
        import matrix_client as mc
        await mc.send_to_user_by_name("dave", report_text)
    except Exception as e:
        log.warning(f"Matrix report failed: {e}")

    return report
