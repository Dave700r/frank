"""Email integration via Proton Bridge IMAP/SMTP.
Read-only by default. Send only when explicitly requested via DM."""
import os
import imaplib
import smtplib
import ssl
import email
import logging
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

log = logging.getLogger("family-bot.email")

import config as app_config

IMAP_HOST = app_config.IMAP_HOST
IMAP_PORT = app_config.IMAP_PORT
SMTP_HOST = app_config.SMTP_HOST
SMTP_PORT = app_config.SMTP_PORT
EMAIL_USER = app_config.EMAIL_USER
EMAIL_PASS = app_config.EMAIL_PASS


def _imap_connect():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    mail = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
    mail.starttls(ssl_context=ctx)
    mail.login(EMAIL_USER, EMAIL_PASS)
    return mail


def _decode_str(s):
    if not s:
        return ""
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result).strip()


def _get_body(msg):
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def get_unread(limit=10):
    """Get unread emails from inbox. Returns list of dicts."""
    mail = _imap_connect()
    try:
        mail.select("INBOX")
        _, ids = mail.search(None, "UNSEEN")
        if not ids[0]:
            return []

        msg_ids = ids[0].split()[-limit:]
        results = []
        for msg_id in msg_ids:
            _, data = mail.fetch(msg_id, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            results.append({
                "id": msg_id.decode(),
                "from": _decode_str(msg["From"]),
                "subject": _decode_str(msg["Subject"]),
                "date": _decode_str(msg["Date"]),
                "body_preview": _get_body(msg)[:500],
            })
        return results
    finally:
        mail.logout()


def get_recent(folder="INBOX", limit=10):
    """Get most recent emails from a folder."""
    mail = _imap_connect()
    try:
        mail.select(folder)
        _, ids = mail.search(None, "ALL")
        if not ids[0]:
            return []

        msg_ids = ids[0].split()[-limit:]
        results = []
        for msg_id in msg_ids:
            _, data = mail.fetch(msg_id, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            results.append({
                "id": msg_id.decode(),
                "from": _decode_str(msg["From"]),
                "subject": _decode_str(msg["Subject"]),
                "date": _decode_str(msg["Date"]),
                "body_preview": _get_body(msg)[:500],
            })
        return results
    finally:
        mail.logout()


def get_bills(limit=10):
    """Get recent emails from the Bills label."""
    return get_recent("Labels/Bills", limit=limit)


def search_inbox(query, limit=10):
    """Search inbox by subject or from."""
    mail = _imap_connect()
    try:
        mail.select("INBOX")
        # Try subject search first
        _, ids = mail.search(None, f'(SUBJECT "{query}")')
        if not ids[0]:
            # Try from search
            _, ids = mail.search(None, f'(FROM "{query}")')
        if not ids[0]:
            return []

        msg_ids = ids[0].split()[-limit:]
        results = []
        for msg_id in msg_ids:
            _, data = mail.fetch(msg_id, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            results.append({
                "id": msg_id.decode(),
                "from": _decode_str(msg["From"]),
                "subject": _decode_str(msg["Subject"]),
                "date": _decode_str(msg["Date"]),
                "body_preview": _get_body(msg)[:500],
            })
        return results
    finally:
        mail.logout()


def send_email(to, subject, body):
    """Send an email as Dave. Only call from DM context."""
    msg = MIMEMultipart()
    msg["From"] = EMAIL_USER
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(EMAIL_USER, EMAIL_PASS)
        s.sendmail(EMAIL_USER, to, msg.as_string())

    log.info(f"Email sent to {to}: {subject}")
    return True


def get_unread_count():
    """Quick check of unread email count."""
    mail = _imap_connect()
    try:
        mail.select("INBOX")
        _, ids = mail.search(None, "UNSEEN")
        return len(ids[0].split()) if ids[0] else 0
    finally:
        mail.logout()


def parse_bill_email(subject, body, sender):
    """Try to extract bill amount and due date from email content.
    Returns dict with payee, amount, due_date or None."""
    import re

    # Common bill patterns
    amount_patterns = [
        r'\$\s*([\d,]+\.?\d*)',
        r'amount\s*(?:due|owing|:)\s*\$?\s*([\d,]+\.?\d*)',
        r'total\s*(?:due|owing|:)\s*\$?\s*([\d,]+\.?\d*)',
        r'balance\s*(?:due|owing|:)\s*\$?\s*([\d,]+\.?\d*)',
    ]

    date_patterns = [
        r'due\s*(?:date|by|on)?\s*:?\s*(\w+ \d{1,2},?\s*\d{4})',
        r'(\d{4}-\d{2}-\d{2})',
        r'(\w+ \d{1,2},?\s*\d{4})',
    ]

    amount = None
    for pattern in amount_patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            amount = float(match.group(1).replace(",", ""))
            break

    due_date = None
    for pattern in date_patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            due_date = match.group(1)
            break

    if amount:
        return {
            "payee": subject or sender,
            "amount": amount,
            "due_date": due_date,
        }
    return None
