"""Permission system for Frank — confirms before high-impact actions.
Actions are classified by risk level. High-risk actions require user confirmation."""
import logging
import asyncio
from datetime import datetime, timedelta

log = logging.getLogger("family-bot.permissions")

# Risk classification
RISK_LEVELS = {
    "add": "low",           # Add to shopping list
    "bought": "low",        # Mark as bought
    "remove": "medium",     # Remove from list (could lose data)
    "remind": "low",        # Set reminder
    "log_spend": "medium",  # Log financial transaction
    "send_message": "medium",  # DM a family member
    "followup": "low",      # Schedule a follow-up
}

# Pending confirmations: chat_id -> {action, expires, callback}
_pending = {}


def needs_confirmation(action: str) -> bool:
    """Check if an action needs user confirmation."""
    risk = RISK_LEVELS.get(action, "low")
    return risk in ("high",)  # Only high-risk needs confirmation for now


def request_confirmation(chat_id: str, action: dict, description: str) -> str:
    """Store a pending confirmation and return the prompt message."""
    _pending[chat_id] = {
        "action": action,
        "description": description,
        "expires": (datetime.now() + timedelta(minutes=5)).isoformat(),
    }
    return f"Confirm: {description}\nReply 'yes' to proceed or 'no' to cancel. (expires in 5 min)"


def check_confirmation(chat_id: str, text: str) -> dict:
    """Check if user is confirming a pending action.
    Returns the action if confirmed, None if denied or no pending."""
    if chat_id not in _pending:
        return None

    pending = _pending[chat_id]

    # Check expiry
    if datetime.now() > datetime.fromisoformat(pending["expires"]):
        del _pending[chat_id]
        return None

    lower = text.lower().strip()
    if lower in ("yes", "y", "confirm", "do it", "go ahead"):
        action = pending["action"]
        del _pending[chat_id]
        return action
    elif lower in ("no", "n", "cancel", "nope", "nevermind"):
        del _pending[chat_id]
        return {"cancelled": True}

    return None  # Not a confirmation response


def get_risk_level(action: str) -> str:
    """Get the risk level for an action type."""
    return RISK_LEVELS.get(action, "low")
