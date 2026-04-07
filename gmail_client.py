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

_services = {}  # keyed by member_name (None = default/global)


def _get_credentials_path():
    """Path to OAuth client credentials (downloaded from Google Cloud Console)."""
    return Path(config._CONFIG_DIR) / "gmail_credentials.json"


def _get_token_path(member_name=None):
    """Path to stored refresh/access token. Per-user tokens use gmail_token_<name>.json."""
    if member_name:
        return Path(config._CONFIG_DIR) / f"gmail_token_{member_name}.json"
    return Path(config._CONFIG_DIR) / "gmail_token.json"


def _get_service(member_name=None):
    """Lazy-init Gmail API service with auto-refresh. Supports per-user tokens."""
    if member_name in _services:
        return _services[member_name]

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_path = _get_token_path(member_name)
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
        log.info(f"Gmail credentials saved for {member_name or 'default'}")

    service = build("gmail", "v1", credentials=creds)
    _services[member_name] = service
    log.info(f"Gmail API connected for {member_name or 'default'}")
    return service


def setup_for_user(member_name):
    """Interactive setup: authorize Gmail for a specific family member.
    Run this from the command line: python -c "import gmail_client; gmail_client.setup_for_user('name')" """
    service = _get_service(member_name)
    if service:
        print(f"Gmail authorized for {member_name}. Token saved to {_get_token_path(member_name)}")
    else:
        print(f"Gmail setup failed for {member_name}.")


def get_auth_url(member_name):
    """Generate a Gmail OAuth authorization URL for a family member.
    Returns the URL string, or None if credentials file is missing."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_path = _get_credentials_path()
    if not creds_path.exists():
        log.error("Gmail OAuth credentials file not found: gmail_credentials.json")
        return None

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_url, _ = flow.authorization_url(prompt="consent")
    # Store the flow temporarily for code exchange
    _pending_flows[member_name] = flow
    log.info(f"Gmail auth URL generated for {member_name}")
    return auth_url


def exchange_auth_code(member_name, code):
    """Exchange an authorization code for tokens. Returns True on success."""
    flow = _pending_flows.pop(member_name, None)
    if not flow:
        log.error(f"No pending auth flow for {member_name}")
        return False

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        token_path = _get_token_path(member_name)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
        # Clear cached service so it picks up new creds
        _services.pop(member_name, None)
        log.info(f"Gmail authorized for {member_name}, token saved to {token_path}")
        return True
    except Exception as e:
        log.error(f"Gmail auth code exchange failed for {member_name}: {e}")
        return False


def is_setup(member_name):
    """Check if Gmail is set up for a member (token file exists)."""
    return _get_token_path(member_name).exists()


_pending_flows = {}  # temporary storage for OAuth flows awaiting code exchange


def get_members_with_gmail():
    """Return list of member names that have Gmail configured."""
    members = []
    for name, member in config.FAMILY_MEMBERS.items():
        em = member.get("email")
        if em and em["type"] == "gmail" and _get_token_path(name).exists():
            members.append(name)
    # Also include default if global gmail is enabled and token exists
    if config.GMAIL_ENABLED and _get_token_path().exists():
        if config.OWNER not in members:
            members.append(config.OWNER)
    return members


def get_unread(limit=5, member_name=None):
    """Get recent unread emails."""
    service = _get_service(member_name)
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


def get_unread_count(member_name=None):
    """Get count of unread emails."""
    service = _get_service(member_name)
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


def search(query, limit=5, member_name=None):
    """Search emails by query string."""
    service = _get_service(member_name)
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


def get_message(msg_id, member_name=None):
    """Get full message content by ID."""
    service = _get_service(member_name)
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


def send_email(to, subject, body, member_name=None):
    """Send an email from the authenticated Gmail account."""
    service = _get_service(member_name)
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


def get_labels(member_name=None):
    """List all Gmail labels."""
    service = _get_service(member_name)
    if not service:
        return []
    try:
        results = service.users().labels().list(userId="me").execute()
        return [{"id": l["id"], "name": l["name"]} for l in results.get("labels", [])]
    except Exception as e:
        log.error(f"Gmail labels error: {e}")
        return []


def get_bills(limit=5, member_name=None):
    """Search for bill-related emails."""
    bill_queries = [
        "subject:(bill OR invoice OR statement OR payment due OR amount owing)",
        "from:(noreply OR billing OR payments OR accounts)",
    ]
    query = " OR ".join(f"({q})" for q in bill_queries)
    return search(f"newer_than:30d ({query})", limit=limit, member_name=member_name)
