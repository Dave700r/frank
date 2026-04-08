"""Email integration via IMAP/SMTP.
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


def _imap_connect(host=None, port=None, user=None, password=None):
    """Connect to IMAP server. Uses SSL for port 993, STARTTLS otherwise."""
    h = host or IMAP_HOST
    p = port or IMAP_PORT
    u = user or EMAIL_USER
    pw = password or EMAIL_PASS
    ctx = ssl.create_default_context()
    if p == 993:
        mail = imaplib.IMAP4_SSL(h, p, ssl_context=ctx)
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        mail = imaplib.IMAP4(h, p)
        mail.starttls(ssl_context=ctx)
    mail.login(u, pw)
    return mail


def _get_user_creds(member_name=None):
    """Get IMAP credentials for a specific family member, or global defaults."""
    if member_name and member_name in app_config.FAMILY_MEMBERS:
        em = app_config.FAMILY_MEMBERS[member_name].get("email")
        if em and em["type"] == "imap":
            # Password can come from env var (pass_env) or directly (password field from chat setup)
            password = em.get("password", "")
            if not password and em.get("pass_env"):
                password = os.environ.get(em["pass_env"], "")
            return {
                "host": em.get("imap_host") or IMAP_HOST,
                "port": em.get("imap_port") or IMAP_PORT,
                "user": em.get("user") or EMAIL_USER,
                "password": password or EMAIL_PASS,
                "smtp_host": em.get("smtp_host") or SMTP_HOST,
                "smtp_port": em.get("smtp_port") or SMTP_PORT,
            }
    return {"host": IMAP_HOST, "port": IMAP_PORT, "user": EMAIL_USER,
            "password": EMAIL_PASS, "smtp_host": SMTP_HOST, "smtp_port": SMTP_PORT}


def get_members_with_email():
    """Return list of member names that have email configured (imap or gmail)."""
    members = []
    for name, member in app_config.FAMILY_MEMBERS.items():
        if member.get("email"):
            members.append(name)
    # If no per-member email but global email is enabled, return owner
    if not members and app_config.EMAIL_ENABLED:
        members.append(app_config.OWNER)
    return members


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
    """Extract plain text body from email message. Falls back to HTML conversion."""
    plain = None
    html = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ct == "text/plain" and not plain:
                plain = text
            elif ct == "text/html" and not html:
                html = text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html = text
            else:
                plain = text

    if plain:
        return plain
    if html:
        return _html_to_text(html)
    return ""


def _html_to_text(html_content):
    """Convert HTML email body to readable plain text."""
    try:
        import html2text
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0
        return h.handle(html_content)
    except ImportError:
        import re
        text = re.sub(r"<br\s*/?>", "\n", html_content, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return text.strip()


def get_unread(limit=10, member_name=None):
    """Get unread emails from inbox. Returns list of dicts."""
    creds = _get_user_creds(member_name)
    mail = _imap_connect(creds["host"], creds["port"], creds["user"], creds["password"])
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
                "body_preview": _get_body(msg)[:2000],
            })
        return results
    finally:
        mail.logout()


def get_recent(folder="INBOX", limit=10, member_name=None):
    """Get most recent emails from a folder."""
    creds = _get_user_creds(member_name)
    mail = _imap_connect(creds["host"], creds["port"], creds["user"], creds["password"])
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
                "body_preview": _get_body(msg)[:2000],
            })
        return results
    finally:
        mail.logout()


def get_bills(limit=10, member_name=None):
    """Get recent emails from the Bills label."""
    return get_recent("Labels/Bills", limit=limit, member_name=member_name)


def search_inbox(query, limit=10, member_name=None):
    """Search inbox by subject or from."""
    creds = _get_user_creds(member_name)
    mail = _imap_connect(creds["host"], creds["port"], creds["user"], creds["password"])
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
                "body_preview": _get_body(msg)[:2000],
            })
        return results
    finally:
        mail.logout()


def get_full_email(msg_id_str, member_name=None):
    """Get the full body of a specific email by message ID."""
    creds = _get_user_creds(member_name)
    mail = _imap_connect(creds["host"], creds["port"], creds["user"], creds["password"])
    try:
        mail.select("INBOX")
        _, data = mail.fetch(msg_id_str.encode() if isinstance(msg_id_str, str) else msg_id_str, "(RFC822)")
        msg = email.message_from_bytes(data[0][1])
        body = _get_body(msg)
        return {
            "id": msg_id_str,
            "from": _decode_str(msg["From"]),
            "subject": _decode_str(msg["Subject"]),
            "date": _decode_str(msg["Date"]),
            "body": body[:3000],
        }
    finally:
        mail.logout()


def send_email(to, subject, body):
    """Send an email as the owner. Only call from DM context."""
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


def get_unread_count(member_name=None):
    """Quick check of unread email count."""
    creds = _get_user_creds(member_name)
    mail = _imap_connect(creds["host"], creds["port"], creds["user"], creds["password"])
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
