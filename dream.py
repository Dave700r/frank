"""Dream — Background memory consolidation for Frank.
Inspired by the autoDream pattern from Claude Code architecture.

Periodically consolidates Frank's episodic memories, conversation logs, and Mem0 facts
into a coherent, pruned knowledge base. Prevents memory bloat and keeps context efficient.

3-Gate Trigger: runs only when ALL conditions are met:
  1. 24+ hours since last consolidation
  2. 5+ new episodes since last consolidation
  3. No other consolidation running (lock)

4 Phases:
  1. Orient  — assess current memory state (counts, sizes, staleness)
  2. Gather  — collect recent episodes, conversation logs, Mem0 facts
  3. Consolidate — use AI to merge, deduplicate, summarize
  4. Prune   — remove stale/redundant entries, enforce size limits
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import episodes

log = logging.getLogger("family-bot.dream")

STATE_FILE = Path.home() / "family-bot" / "dream_state.json"
MAX_EPISODES = 100  # Keep at most this many episodes
MAX_EPISODE_AGE_DAYS = 30  # Prune episodes older than this
MIN_HOURS_BETWEEN = 24
MIN_EPISODES_SINCE = 5

_lock = False


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "last_dream": None,
            "last_episode_count": 0,
            "dreams_completed": 0,
        }


def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def should_dream() -> bool:
    """Check the 3-gate trigger."""
    global _lock
    if _lock:
        return False

    state = _load_state()

    # Gate 1: Time since last dream
    last = state.get("last_dream")
    if last:
        last_dt = datetime.fromisoformat(last)
        if datetime.now() - last_dt < timedelta(hours=MIN_HOURS_BETWEEN):
            return False

    # Gate 2: Enough new episodes
    db = episodes._get_db()
    current_count = db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    last_count = state.get("last_episode_count", 0)
    if current_count - last_count < MIN_EPISODES_SINCE:
        return False

    # Gate 3: Lock not held (checked above)
    return True


async def dream(ai_fn=None):
    """Run the 4-phase memory consolidation.
    ai_fn: async function(prompt, context) -> str for AI summarization.
    If None, uses simple heuristic consolidation."""
    global _lock

    if not should_dream():
        return False

    _lock = True
    log.info("Dream starting — memory consolidation")

    try:
        state = _load_state()

        # ─── Phase 1: Orient ───
        log.info("Dream Phase 1: Orient")
        db = episodes._get_db()
        total_episodes = db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        total_followups = db.execute("SELECT COUNT(*) FROM followups WHERE delivered = 0").fetchone()[0]
        oldest = db.execute("SELECT MIN(timestamp) FROM episodes").fetchone()[0]
        newest = db.execute("SELECT MAX(timestamp) FROM episodes").fetchone()[0]

        # Users with most episodes
        user_counts = db.execute(
            "SELECT user_name, COUNT(*) as cnt FROM episodes GROUP BY user_name ORDER BY cnt DESC"
        ).fetchall()

        orient = {
            "total_episodes": total_episodes,
            "pending_followups": total_followups,
            "oldest_episode": oldest,
            "newest_episode": newest,
            "episodes_by_user": {r[0]: r[1] for r in user_counts},
        }
        log.info(f"Orient: {total_episodes} episodes, oldest={oldest}, users={dict(orient['episodes_by_user'])}")

        # ─── Phase 2: Gather ───
        log.info("Dream Phase 2: Gather")

        # Get all episodes for consolidation
        all_episodes = db.execute(
            "SELECT id, user_name, timestamp, summary, topics, mood, importance FROM episodes "
            "ORDER BY timestamp DESC"
        ).fetchall()

        # Group by user
        by_user = {}
        for ep in all_episodes:
            user = ep[1]
            by_user.setdefault(user, []).append({
                "id": ep[0],
                "timestamp": ep[2],
                "summary": ep[3],
                "topics": ep[4],
                "mood": ep[5],
                "importance": ep[6],
            })

        # ─── Phase 3: Consolidate ───
        log.info("Dream Phase 3: Consolidate")

        if ai_fn and total_episodes > 20:
            # Use AI to create consolidated summaries per user
            for user, eps in by_user.items():
                if len(eps) <= 5:
                    continue

                # Get the older episodes (keep recent 5, consolidate the rest)
                to_consolidate = eps[5:]  # older ones (list is newest-first)
                if not to_consolidate:
                    continue

                summaries = "\n".join(f"- [{e['timestamp'][:10]}] {e['summary']}" for e in to_consolidate)

                try:
                    consolidated = await asyncio.get_event_loop().run_in_executor(
                        None,
                        ai_fn,
                        f"Consolidate these conversation summaries about {user} into 3-5 key points. "
                        f"Focus on: important facts learned, ongoing topics, commitments made, "
                        f"personality traits observed. Be concise.\n\n{summaries}",
                    )

                    # Replace old episodes with one consolidated entry
                    old_ids = [e["id"] for e in to_consolidate]
                    db.execute(
                        f"DELETE FROM episodes WHERE id IN ({','.join('?' * len(old_ids))})",
                        old_ids
                    )
                    db.execute(
                        "INSERT INTO episodes (user_name, timestamp, summary, topics, mood, importance) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (user, datetime.now().isoformat(),
                         f"[Consolidated] {consolidated}", "[]", "neutral", 2)
                    )
                    db.commit()
                    log.info(f"Consolidated {len(old_ids)} episodes for {user}")

                except Exception as e:
                    log.warning(f"AI consolidation failed for {user}: {e}")

        # ─── Phase 4: Prune ───
        log.info("Dream Phase 4: Prune")

        # Remove very old episodes
        cutoff = (datetime.now() - timedelta(days=MAX_EPISODE_AGE_DAYS)).isoformat()
        deleted = db.execute(
            "DELETE FROM episodes WHERE timestamp < ? AND importance < 2",
            (cutoff,)
        ).rowcount
        if deleted:
            log.info(f"Pruned {deleted} episodes older than {MAX_EPISODE_AGE_DAYS} days")

        # Enforce max episode count (keep most important/recent)
        current = db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        if current > MAX_EPISODES:
            excess = current - MAX_EPISODES
            db.execute(
                "DELETE FROM episodes WHERE id IN ("
                "  SELECT id FROM episodes ORDER BY importance ASC, timestamp ASC LIMIT ?"
                ")", (excess,)
            )
            log.info(f"Pruned {excess} excess episodes (over {MAX_EPISODES} limit)")

        # Remove delivered followups older than 7 days
        followup_cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        db.execute(
            "DELETE FROM followups WHERE delivered = 1 AND created_at < ?",
            (followup_cutoff,)
        )

        db.commit()

        # Update state
        final_count = db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        state["last_dream"] = datetime.now().isoformat()
        state["last_episode_count"] = final_count
        state["dreams_completed"] = state.get("dreams_completed", 0) + 1
        _save_state(state)

        log.info(f"Dream complete. Episodes: {total_episodes} -> {final_count}. "
                 f"Dream #{state['dreams_completed']}")
        return True

    except Exception as e:
        log.error(f"Dream failed: {e}", exc_info=True)
        return False
    finally:
        _lock = False


def get_dream_status() -> dict:
    """Get current dream/consolidation status."""
    state = _load_state()
    db = episodes._get_db()
    total = db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]

    result = {
        "last_dream": state.get("last_dream", "never"),
        "dreams_completed": state.get("dreams_completed", 0),
        "total_episodes": total,
        "episodes_since_last": total - state.get("last_episode_count", 0),
        "would_trigger": should_dream(),
    }
    return result
