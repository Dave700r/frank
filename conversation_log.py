"""Conversation logging and memory system for Frank.
Logs conversations to daily-logs/ for the memory watcher to index into Chroma.
Also supports direct memory writes for important facts."""
import os
import json
import logging
import httpx
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict

log = logging.getLogger("family-bot.memory")

import config as app_config

DAILY_LOGS_DIR = app_config.DAILY_LOGS_DIR
MEMORY_DIR = app_config.MEMORY_DIR
MCP_INDEX_URL = app_config.MCP_INDEX_URL

# In-memory buffer for today's conversations
_today_buffer = defaultdict(list)  # user_name -> [(timestamp, user_msg, frank_reply)]


def log_interaction(user_name, user_message, frank_reply):
    """Buffer a conversation turn. Called after every AI response."""
    timestamp = datetime.now().strftime("%H:%M")
    _today_buffer[user_name].append((timestamp, user_message, frank_reply))
    log.debug(f"Buffered interaction with {user_name} at {timestamp}")


def get_today_summary():
    """Get a summary of today's conversations so far."""
    if not _today_buffer:
        return "No conversations today."

    lines = []
    for user, interactions in _today_buffer.items():
        lines.append(f"\n### {user.title()} ({len(interactions)} messages)")
        for ts, msg, reply in interactions:
            lines.append(f"- [{ts}] {user}: {msg[:100]}")
            lines.append(f"  Frank: {reply[:100]}")
    return "\n".join(lines)


def write_daily_log():
    """Write today's conversation log to disk. Called at end of day or on shutdown."""
    if not _today_buffer:
        return

    today = date.today().isoformat()
    log_path = DAILY_LOGS_DIR / f"{today}.md"
    DAILY_LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Build log content
    lines = [f"# Daily Log - {today}\n"]
    lines.append("## Conversations\n")

    total_interactions = 0
    topics = set()

    for user, interactions in _today_buffer.items():
        total_interactions += len(interactions)
        lines.append(f"### {user.title()} ({len(interactions)} messages)\n")
        for ts, msg, reply in interactions:
            lines.append(f"**[{ts}] {user}:** {msg}")
            lines.append(f"**Frank:** {reply}\n")
            # Extract potential topics from messages
            for word in msg.lower().split():
                if len(word) > 4 and word not in ("about", "could", "would", "should", "there", "their", "these", "those", "frank", "please"):
                    topics.add(word)

    lines.insert(2, f"Total interactions: {total_interactions}")
    lines.insert(3, f"Family members active: {', '.join(u.title() for u in _today_buffer.keys())}")
    if topics:
        lines.insert(4, f"Topics: {', '.join(list(topics)[:15])}\n")

    # Append to existing log (don't overwrite — cron jobs may have written earlier)
    mode = "a" if log_path.exists() else "w"
    with open(log_path, mode) as f:
        if mode == "a":
            f.write("\n\n---\n\n")
        f.write("\n".join(lines))

    log.info(f"Daily log written: {log_path} ({total_interactions} interactions)")

    # The memory watcher will auto-detect the file change and re-index


def save_memory(content, memory_type="learned"):
    """Save an important fact to Frank's long-term memory.
    Written to memory/ dir where the watcher will index it."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    filename = f"frank_{memory_type}_{timestamp}.md"
    filepath = MEMORY_DIR / filename

    with open(filepath, "w") as f:
        f.write(f"# Frank Memory - {memory_type.title()}\n")
        f.write(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(content)

    log.info(f"Memory saved: {filepath}")
    return filepath


def extract_and_save_learnings(user_name, message, reply):
    """Analyze a conversation turn for things worth remembering long-term.
    Called for every interaction — uses simple heuristics, not AI."""
    lower = message.lower()

    # Preferences and facts worth remembering
    memory_triggers = {
        "i like": "preference",
        "i don't like": "preference",
        "i prefer": "preference",
        "i hate": "preference",
        "my favorite": "preference",
        "allergic to": "health",
        "can't eat": "health",
        "don't eat": "health",
        "remember that": "explicit",
        "frank remember": "explicit",
        "important:": "explicit",
        "we always": "routine",
        "we never": "routine",
        "every week": "routine",
        "every month": "routine",
    }

    for trigger, mem_type in memory_triggers.items():
        if trigger in lower:
            content = (
                f"**Learned from {user_name.title()}:**\n"
                f"Context: \"{message}\"\n"
                f"Frank's response: \"{reply[:200]}\"\n"
            )
            save_memory(content, memory_type=mem_type)
            log.info(f"Auto-saved {mem_type} memory from {user_name}")
            return True

    return False


def flush_buffer():
    """Flush the conversation buffer. Called on shutdown."""
    if _today_buffer:
        write_daily_log()
        _today_buffer.clear()
