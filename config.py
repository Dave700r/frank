"""Family Bot Configuration — loads from config.yaml"""
import os
import sys
from pathlib import Path

import yaml

# Find config.yaml next to this file
_CONFIG_DIR = Path(__file__).parent
_CONFIG_FILE = _CONFIG_DIR / "config.yaml"

if not _CONFIG_FILE.exists():
    if sys.stdin.isatty():
        print("No config.yaml found — starting setup wizard...\n")
        import setup
        setup.run()
    else:
        print("ERROR: config.yaml not found. Run 'python setup.py' or copy config.yaml.example to config.yaml.")
        sys.exit(1)

with open(_CONFIG_FILE) as f:
    _cfg = yaml.safe_load(f)

# --- Bot ---
BOT_NAME = _cfg["bot"]["name"]
MATRIX_HOMESERVER = _cfg["bot"]["matrix_homeserver"]
MATRIX_BOT_USER = _cfg["bot"]["matrix_bot_user"]
MATRIX_FAMILY_ROOM_ID = _cfg["bot"]["matrix_family_room_id"]

# --- Family ---
OWNER = _cfg["family"]["owner"]
FAMILY_MEMBERS = {}
for name, member in _cfg["family"]["members"].items():
    FAMILY_MEMBERS[name] = {
        "matrix_id": member["matrix_id"],
        "nickname": member["nickname"],
    }
    if "telegram_id" in member:
        FAMILY_MEMBERS[name]["telegram_id"] = member["telegram_id"]
    # Per-member email config (optional)
    if "email" in member:
        _em = member["email"]
        FAMILY_MEMBERS[name]["email"] = {
            "type": _em.get("type", "imap"),  # "imap" or "gmail"
            "imap_host": _em.get("imap_host", ""),
            "imap_port": _em.get("imap_port", 993),
            "smtp_host": _em.get("smtp_host", ""),
            "smtp_port": _em.get("smtp_port", 587),
            "user": _em.get("user", ""),
            "pass_env": _em.get("pass_env", ""),  # env var name for password
        }

# Load runtime email accounts (set up via chat, stored in email_accounts.json)
_email_accounts_file = _CONFIG_DIR / "email_accounts.json"
if _email_accounts_file.exists():
    import json as _json
    with open(_email_accounts_file) as _f:
        _runtime_accounts = _json.load(_f)
    for _name, _acct in _runtime_accounts.items():
        if _name in FAMILY_MEMBERS and "email" not in FAMILY_MEMBERS[_name]:
            FAMILY_MEMBERS[_name]["email"] = _acct

# Reverse lookups
TELEGRAM_ID_TO_NAME = {
    v["telegram_id"]: k for k, v in FAMILY_MEMBERS.items() if "telegram_id" in v
}
MATRIX_ID_TO_NAME = {v["matrix_id"]: k for k, v in FAMILY_MEMBERS.items()}

# --- Paths ---
_paths = _cfg.get("paths", {})
_data_dir = Path(_paths.get("data_dir", _CONFIG_DIR / "data"))

def _resolve_path(key, default):
    val = _paths.get(key, default)
    p = Path(val)
    if p.is_absolute():
        return p
    return _data_dir / p

WORKSPACE = _data_dir
INVENTORY_DB = _resolve_path("inventory_db", "inventory.db")
FINANCE_DB = _resolve_path("finance_db", "finance.db")
SPEND_LOG = _resolve_path("spend_log", "spend-log.json")
PAYMENT_TRACKER = _resolve_path("payment_tracker", "payment_tracker.json")
RECIPE_DB = _resolve_path("recipe_db", "recipes.db")
REMINDERS_DB = _resolve_path("reminders_db", "reminders.db")
DAILY_LOGS_DIR = _resolve_path("daily_logs_dir", "daily-logs")
MEMORY_DIR = _resolve_path("memory_dir", "memory")
BRIEFING_SCRIPTS_DIR = WORKSPACE

# --- Telegram (optional) ---
_telegram = _cfg.get("telegram", {})
TELEGRAM_ENABLED = _telegram.get("enabled", False)
TELEGRAM_TOKEN_FILE = _CONFIG_DIR / "telegram_token.txt"
FAMILY_GROUP_ID = _telegram.get("family_group_id", "")

# --- Location ---
_loc = _cfg.get("location", {})
LATITUDE = _loc.get("latitude", 0.0)
LONGITUDE = _loc.get("longitude", 0.0)
TIMEZONE = _loc.get("timezone", "UTC")

# --- AI ---
AI_MODEL = _cfg.get("ai", {}).get("model", "anthropic/claude-haiku-4.5")

# --- Features ---
GROCERY_ENABLED = _cfg.get("grocery", {}).get("enabled", True)
RECIPES_ENABLED = _cfg.get("recipes", {}).get("enabled", True)
BUDDY_ENABLED = _cfg.get("buddy", {}).get("enabled", True)
FINANCE_ENABLED = _cfg.get("finance", {}).get("enabled", True)

# --- Firefly III ---
_firefly = _cfg.get("firefly", {})
FIREFLY_ENABLED = _firefly.get("enabled", False)
FIREFLY_BASE = _firefly.get("base_url", "")
FIREFLY_TOKEN = os.environ.get("FIREFLY_TOKEN", "")
FIREFLY_ACCOUNTS = _firefly.get("accounts", {})

# --- Email (owner's inbox) ---
_email = _cfg.get("email", {})
EMAIL_ENABLED = _email.get("enabled", False)
IMAP_HOST = _email.get("imap_host", "")
IMAP_PORT = _email.get("imap_port", 1143)
SMTP_HOST = _email.get("smtp_host", "")
SMTP_PORT = _email.get("smtp_port", 1025)
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")

# --- Gmail (alternative to IMAP) ---
_gmail = _cfg.get("gmail", {})
GMAIL_ENABLED = _gmail.get("enabled", False)

# --- Immich ---
_immich = _cfg.get("immich", {})
IMMICH_ENABLED = _immich.get("enabled", False)
IMMICH_BASE_URL = _immich.get("base_url", "")
IMMICH_API_KEY = _immich.get("api_key", "")

# --- AgentMail (bot's own email) ---
_agentmail = _cfg.get("agentmail", {})
AGENTMAIL_ENABLED = _agentmail.get("enabled", False)
AGENTMAIL_ADDRESS = _agentmail.get("address", "")

# --- Mem0 ---
_mem0 = _cfg.get("mem0", {})
MEM0_ENABLED = _mem0.get("enabled", False)
MEM0_OLLAMA_BASE_URL = _mem0.get("ollama_base_url", "")
MEM0_OLLAMA_MODEL = _mem0.get("ollama_model", "nomic-embed-text")
MEM0_LLM_MODEL = _mem0.get("llm_model", "google/gemini-2.0-flash-001")
MEM0_SKIP_SSL_VERIFY = _mem0.get("skip_ssl_verify", False)
MEM0_DATA_DIR = _resolve_path("mem0_data", "mem0_data") if "mem0_data" not in _paths else _resolve_path("mem0_data", "mem0_data")

# --- MCP ---
MCP_INDEX_URL = _cfg.get("mcp", {}).get("index_url", "http://localhost:8765/index")

# --- Voice ---
_voice = _cfg.get("voice", {})
VOICE_ENABLED = _voice.get("enabled", False)
VOICE_HOST = _voice.get("host", "127.0.0.1")
VOICE_PORT = _voice.get("port", 5123)

# --- Persona ---
PERSONA_FILE = _cfg.get("persona_file")
SPANISH_LEARNERS = _cfg.get("spanish_learners", [])
