"""Gmail API integration — OAuth2-based alternative to IMAP.
First run requires interactive browser auth to get refresh token.
After that, runs headless using stored credentials."""
import os
import json
import base64
import logging
from pathlib import Path
from email.mime.text import MIMEText
from datetime import datetime

import config

log = logging.getLogger("family-bot.gmail")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.labels",
]

_service = None


def _get_credentials_path():
    """Path to OAuth client credentials (downloaded from Google Cloud Console)."""
    return Path(config._CONFIG_DIR) / "gmail_credentials.json"


def _get_token_path():
    """Path to stored refresh/access token."""
    return Path(config._CONFIG_DIR) / "gmail_token.json"


def _get_service():
    """Lazy-init Gmail API service with auto-refresh."""
    global _service
    if _service is not None:
        return _service

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_path = _get_token_path()
    creds_path = _get_credentials_path()

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                log.error(
                    "Gmail setup required: download OAuth credentials from "
                    "Google Cloud Console and save as gmail_credentials.json"
                )
                return None
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as f:
            f.write(creds.to_json())
        log.info("Gmail credentials saved")

    _service = build("gmail", "v1", credentials=creds)
    log.info("Gmail API connected")
    return _service


def get_unread(limit=5):
    """Get recent unread emails."""
    service = _get_service()
    if not service:
        return []

    try:
        results = service.users().messages().list(
            userId="me", q="is:unread", maxResults=limit
        ).execute()
        messages = results.get("messages", [])

        emails = []
        for msg in messages:
            detail = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]
            ).execute()

            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            emails.append({
                "id": msg["id"],
                "from": headers.get("From", "Unknown"),
                "subject": headers.get("Subject", "(no subject)"),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
            })

        return emails
    except Exception as e:
        log.error(f"Gmail fetch error: {e}")
        return []


def get_unread_count():
    """Get count of unread emails."""
    service = _get_service()
    if not service:
        return 0
    try:
        results = service.users().messages().list(
            userId="me", q="is:unread", maxResults=1
        ).execute()
        return results.get("resultSizeEstimate", 0)
    except Exception as e:
        log.error(f"Gmail count error: {e}")
        return 0


def search(query, limit=5):
    """Search emails by query string."""
    service = _get_service()
    if not service:
        return []

    try:
        results = service.users().messages().list(
            userId="me", q=query, maxResults=limit
        ).execute()
        messages = results.get("messages", [])

        emails = []
        for msg in messages:
            detail = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]
            ).execute()
            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            emails.append({
                "id": msg["id"],
                "from": headers.get("From", "Unknown"),
                "subject": headers.get("Subject", "(no subject)"),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
            })
        return emails
    except Exception as e:
        log.error(f"Gmail search error: {e}")
        return []


def get_message(msg_id):
    """Get full message content by ID."""
    service = _get_service()
    if not service:
        return None

    try:
        detail = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}

        # Extract body text
        body = ""
        payload = detail.get("payload", {})
        if "body" in payload and payload["body"].get("data"):
            body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        elif "parts" in payload:
            for part in payload["parts"]:
                if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                    body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                    break

        return {
            "id": msg_id,
            "from": headers.get("From", "Unknown"),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date", ""),
            "body": body,
            "snippet": detail.get("snippet", ""),
        }
    except Exception as e:
        log.error(f"Gmail message error: {e}")
        return None


def send_email(to, subject, body):
    """Send an email from the authenticated Gmail account."""
    service = _get_service()
    if not service:
        raise RuntimeError("Gmail not configured")

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    try:
        result = service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        log.info(f"Gmail sent to {to}: {subject}")
        return result
    except Exception as e:
        log.error(f"Gmail send error: {e}")
        raise


def get_labels():
    """List all Gmail labels."""
    service = _get_service()
    if not service:
        return []
    try:
        results = service.users().labels().list(userId="me").execute()
        return [{"id": l["id"], "name": l["name"]} for l in results.get("labels", [])]
    except Exception as e:
        log.error(f"Gmail labels error: {e}")
        return []


def get_bills(limit=5):
    """Search for bill-related emails."""
    bill_queries = [
        "subject:(bill OR invoice OR statement OR payment due OR amount owing)",
        "from:(noreply OR billing OR payments OR accounts)",
    ]
    query = " OR ".join(f"({q})" for q in bill_queries)
    return search(f"newer_than:30d ({query})", limit=limit)
