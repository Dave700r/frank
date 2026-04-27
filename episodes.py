"""Episodic memory and follow-up system for Frank.
Stores conversation episode summaries and schedules natural follow-ups."""
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("family-bot.episodes")

DB_PATH = Path.home() / "family-bot" / "episodes.db"

_db = None


def _get_db():
    global _db
    if _db is None:
        _db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA synchronous=NORMAL")
        _db.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY,
                user_name TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                summary TEXT NOT NULL,
                topics TEXT DEFAULT '[]',
                mood TEXT DEFAULT 'neutral',
                importance INTEGER DEFAULT 1,
                chat_id TEXT
            )
        """)
        _db.execute("""
            CREATE TABLE IF NOT EXISTS followups (
                id INTEGER PRIMARY KEY,
                user_name TEXT NOT NULL,
                topic TEXT NOT NULL,
                question TEXT NOT NULL,
                follow_up_after TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered INTEGER DEFAULT 0,
                episode_id INTEGER,
                FOREIGN KEY (episode_id) REFERENCES episodes(id)
            )
        """)
        _db.execute("""
            CREATE INDEX IF NOT EXISTS idx_episodes_user
            ON episodes(user_name, timestamp DESC)
        """)
        _db.execute("""
            CREATE INDEX IF NOT EXISTS idx_followups_pending
            ON followups(delivered, follow_up_after)
        """)
        _db.commit()
    return _db


def store_episode(user_name: str, summary: str, topics: list = None,
                  mood: str = "neutral", importance: int = 1, chat_id: str = ""):
    """Store a conversation episode summary."""
    db = _get_db()
    db.execute(
        "INSERT INTO episodes (user_name, timestamp, summary, topics, mood, importance, chat_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_name.lower(), datetime.now().isoformat(), summary,
         json.dumps(topics or []), mood, importance, chat_id)
    )
    db.commit()
    log.info(f"Episode stored for {user_name}: {summary[:60]}...")


def recall_episodes(user_name: str = None, topic: str = None, limit: int = 5) -> list:
    """Recall recent episodes, optionally filtered by user or topic."""
    db = _get_db()
    if user_name and topic:
        rows = db.execute(
            "SELECT summary, timestamp, topics, mood FROM episodes "
            "WHERE user_name = ? AND topics LIKE ? "
            "ORDER BY importance DESC, timestamp DESC LIMIT ?",
            (user_name.lower(), f'%{topic}%', limit)
        ).fetchall()
    elif user_name:
        rows = db.execute(
            "SELECT summary, timestamp, topics, mood FROM episodes "
            "WHERE user_name = ? ORDER BY timestamp DESC LIMIT ?",
            (user_name.lower(), limit)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT summary, timestamp, topics, mood FROM episodes "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def recall_recent_for_context(user_name: str, limit: int = 3) -> str:
    """Get a formatted string of recent episodes for injection into AI context."""
    episodes = recall_episodes(user_name=user_name, limit=limit)
    if not episodes:
        return ""

    lines = []
    for ep in episodes:
        ts = ep["timestamp"][:10]  # just the date
        lines.append(f"- [{ts}] {ep['summary']}")
    return "Recent interactions with this person:\n" + "\n".join(lines)


# ─── Follow-ups ───

def schedule_followup(user_name: str, topic: str, question: str,
                      delay_hours: float = 24.0, episode_id: int = None):
    """Schedule a follow-up question for later."""
    db = _get_db()
    follow_up_after = (datetime.now() + timedelta(hours=delay_hours)).isoformat()
    db.execute(
        "INSERT INTO followups (user_name, topic, question, follow_up_after, created_at, episode_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_name.lower(), topic, question, follow_up_after,
         datetime.now().isoformat(), episode_id)
    )
    db.commit()
    log.info(f"Follow-up scheduled for {user_name} in {delay_hours}h: {question[:60]}")


def get_due_followups() -> list:
    """Get all follow-ups that are due for delivery."""
    db = _get_db()
    now = datetime.now().isoformat()
    rows = db.execute(
        "SELECT id, user_name, topic, question FROM followups "
        "WHERE delivered = 0 AND follow_up_after <= ? "
        "ORDER BY follow_up_after",
        (now,)
    ).fetchall()
    return [dict(r) for r in rows]


def mark_followup_delivered(followup_id: int):
    """Mark a follow-up as delivered."""
    db = _get_db()
    db.execute("UPDATE followups SET delivered = 1 WHERE id = ?", (followup_id,))
    db.commit()


def get_pending_followups_for_user(user_name: str) -> list:
    """Get undelivered follow-ups for a user (for context injection)."""
    db = _get_db()
    rows = db.execute(
        "SELECT topic, question, follow_up_after FROM followups "
        "WHERE user_name = ? AND delivered = 0 ORDER BY follow_up_after",
        (user_name.lower(),)
    ).fetchall()
    return [dict(r) for r in rows]
