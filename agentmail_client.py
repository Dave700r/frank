"""AgentMail integration — bot's own email inbox."""
import os
import logging
import config

from agentmail import AgentMail

log = logging.getLogger("family-bot.agentmail")

INBOX_ID = config.AGENTMAIL_ADDRESS

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("AGENTMAIL_API_KEY", "")
        if not api_key:
            raise RuntimeError("AGENTMAIL_API_KEY not set")
        _client = AgentMail(api_key=api_key)
    return _client


def get_unread(limit=10):
    """Get recent messages."""
    client = _get_client()
    msgs = client.inboxes.messages.list(INBOX_ID, limit=limit)
    results = []
    for m in msgs.messages:
        # Skip sent messages
        if "sent" in (m.labels or []):
            continue
        results.append({
            "from": m.from_,
            "to": m.to,
            "subject": m.subject or "(no subject)",
            "preview": m.preview or "",
            "date": str(m.timestamp),
            "message_id": m.message_id,
            "thread_id": m.thread_id,
        })
    return results


def get_unread_count():
    """Get count of unread messages."""
    msgs = get_unread(limit=50)
    return len(msgs)


def get_all_messages(limit=20):
    """Get all recent messages including sent."""
    client = _get_client()
    msgs = client.inboxes.messages.list(INBOX_ID, limit=limit)
    results = []
    for m in msgs.messages:
        results.append({
            "from": m.from_,
            "to": m.to,
            "subject": m.subject or "(no subject)",
            "preview": m.preview or "",
            "date": str(m.timestamp),
            "labels": m.labels or [],
            "message_id": m.message_id,
            "thread_id": m.thread_id,
        })
    return results


def send_email(to: str, subject: str, body: str):
    """Send an email from Frank's inbox."""
    client = _get_client()
    msg = client.inboxes.messages.send(
        INBOX_ID,
        to=to if isinstance(to, list) else [to],
        subject=subject,
        text=body,
    )
    log.info(f"Email sent to {to}: {subject}")
    return msg


def reply_to(thread_id: str, body: str):
    """Reply to an existing thread."""
    client = _get_client()
    # Get the thread to find the last message
    thread = client.inboxes.threads.get(INBOX_ID, thread_id)
    if thread.messages:
        last_msg = thread.messages[-1]
        reply_to_addr = last_msg.from_
        subject = f"Re: {last_msg.subject}" if not last_msg.subject.startswith("Re:") else last_msg.subject
        msg = client.inboxes.messages.send(
            INBOX_ID,
            to=[reply_to_addr] if isinstance(reply_to_addr, str) else reply_to_addr,
            subject=subject,
            text=body,
            in_reply_to=last_msg.message_id,
        )
        log.info(f"Reply sent in thread {thread_id}")
        return msg
    return None


def read_email(message_id: str) -> dict:
    """Read the full content of a specific email."""
    client = _get_client()
    msg = client.inboxes.messages.get(INBOX_ID, message_id)
    return {
        "from": msg.from_,
        "to": msg.to,
        "subject": msg.subject or "(no subject)",
        "body": msg.extracted_text or msg.text or msg.preview or "",
        "html": msg.html or "",
        "date": str(msg.timestamp),
        "message_id": msg.message_id,
        "thread_id": msg.thread_id,
        "labels": msg.labels or [],
    }


def get_recent_with_content(limit=5) -> str:
    """Get recent emails with full content, formatted for AI context."""
    client = _get_client()
    msgs = client.inboxes.messages.list(INBOX_ID, limit=limit)

    lines = []
    for m in msgs.messages:
        if "sent" in (m.labels or []):
            continue
        # Get full content
        try:
            full = client.inboxes.messages.get(INBOX_ID, m.message_id)
            body = (full.extracted_text or full.text or full.preview or "")[:500]
        except Exception:
            body = m.preview or ""

        lines.append(
            f"From: {m.from_}\n"
            f"Subject: {m.subject}\n"
            f"Date: {str(m.timestamp)[:16]}\n"
            f"Body: {body}\n"
            f"Message-ID: {m.message_id}\n"
        )

    return "\n---\n".join(lines) if lines else "No emails."


def search(query: str, limit=10):
    """Search emails."""
    client = _get_client()
    try:
        results = client.inboxes.messages.list(INBOX_ID, limit=limit)
        # Filter client-side since the SDK might not have search
        matches = []
        for m in results.messages:
            q = query.lower()
            if (q in (m.subject or "").lower() or
                q in (m.preview or "").lower() or
                q in (m.from_ or "").lower()):
                matches.append({
                    "from": m.from_,
                    "subject": m.subject,
                    "preview": m.preview,
                    "date": str(m.timestamp),
                })
        return matches
    except Exception as e:
        log.error(f"Email search error: {e}")
        return []


def get_inbox_info():
    """Get inbox status info."""
    client = _get_client()
    inbox = client.inboxes.get(INBOX_ID)
    return {
        "email": inbox.email,
        "display_name": inbox.display_name,
        "created_at": str(inbox.created_at),
    }
