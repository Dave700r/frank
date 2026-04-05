"""Conversational style learning for Frank.
Builds per-user style profiles based on interaction patterns.
Uses LLM-based profile extraction every Nth interaction, not every message.

Architecture follows 2025 research on prompt-based personality adaptation:
- JSON user profiles stored per user
- Implicit feedback signals (engagement, re-asks, drop-offs)
- Periodic LLM-based profile updates (every 5 interactions)
- Profiles injected into system prompt (<500 tokens)
- Decay/averaging to prevent over-fitting to recent behavior"""
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("family-bot.style")

PROFILES_DIR = Path.home() / "family-bot" / "profiles"
PROFILES_DIR.mkdir(exist_ok=True)

UPDATE_EVERY_N = 5  # Update profile every N interactions
_interaction_counts = {}  # user -> count since last update
_recent_interactions = {}  # user -> list of recent (user_msg, frank_reply, engaged)


def _profile_path(user_name: str) -> Path:
    return PROFILES_DIR / f"{user_name.lower()}.json"


def _load_profile(user_name: str) -> dict:
    path = _profile_path(user_name)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "user_name": user_name.lower(),
            "response_style": "balanced",
            "tone": "match Frank's personality",
            "detail_level": "moderate",
            "likes_followup_questions": True,
            "likes_humor": True,
            "topics_of_interest": [],
            "communication_notes": [],
            "total_interactions": 0,
            "last_updated": None,
            "created_at": datetime.now().isoformat(),
        }


def _save_profile(user_name: str, profile: dict):
    path = _profile_path(user_name)
    with open(path, "w") as f:
        json.dump(profile, f, indent=2)


def log_interaction(user_name: str, user_msg: str, frank_reply: str):
    """Log an interaction for style learning. Called after every AI response."""
    key = user_name.lower()

    # Track recent interactions for batch analysis
    if key not in _recent_interactions:
        _recent_interactions[key] = []

    _recent_interactions[key].append({
        "user_msg": user_msg[:300],
        "frank_reply": frank_reply[:300],
        "user_msg_words": len(user_msg.split()),
        "frank_reply_words": len(frank_reply.split()),
        "timestamp": datetime.now().isoformat(),
        "engaged": False,  # Updated by mark_engaged()
    })

    # Keep only last 20 interactions
    _recent_interactions[key] = _recent_interactions[key][-20:]

    # Increment counter
    _interaction_counts[key] = _interaction_counts.get(key, 0) + 1

    # Update total in profile
    profile = _load_profile(key)
    profile["total_interactions"] = profile.get("total_interactions", 0) + 1
    _save_profile(key, profile)


def mark_engaged(user_name: str):
    """Mark that the user sent a follow-up (engaged with Frank's last response)."""
    key = user_name.lower()
    if key in _recent_interactions and _recent_interactions[key]:
        _recent_interactions[key][-1]["engaged"] = True


def should_update_profile(user_name: str) -> bool:
    """Check if we should run an LLM profile update."""
    key = user_name.lower()
    count = _interaction_counts.get(key, 0)
    return count >= UPDATE_EVERY_N


def update_profile_with_llm(user_name: str, llm_fn) -> bool:
    """Run LLM-based profile update using recent interactions.
    llm_fn: callable(prompt) -> str"""
    key = user_name.lower()
    recent = _recent_interactions.get(key, [])
    if not recent:
        return False

    profile = _load_profile(key)

    # Build interaction digest
    digest_lines = []
    for i in recent[-10:]:  # Last 10 interactions
        engaged = "continued talking" if i["engaged"] else "went quiet after"
        digest_lines.append(
            f"- User ({i['user_msg_words']} words): {i['user_msg'][:150]}\n"
            f"  Frank ({i['frank_reply_words']} words): {i['frank_reply'][:150]}\n"
            f"  Result: {engaged}"
        )
    digest = "\n".join(digest_lines)

    prompt = f"""Analyze these recent conversations with {user_name} and update their style profile.

Current profile:
{json.dumps(profile, indent=2)}

Recent interactions:
{digest}

Based on the interaction patterns, update the profile JSON. Consider:
- Do they prefer short or detailed responses?
- Do they engage more when asked follow-up questions or when given direct answers?
- What tone works best? (casual, technical, warm, direct)
- What topics come up most?
- Any communication notes? (e.g., "uses voice-to-text", "asks rapid-fire questions")

IMPORTANT: Don't over-correct from one interaction. Look at the overall pattern.
Return ONLY valid JSON with the same fields as the current profile. Update the fields that need changing, keep the rest."""

    try:
        result = llm_fn(prompt)

        # Parse the JSON from the response
        # Strip markdown code fences if present
        result = result.strip()
        if result.startswith("```"):
            result = result.split("\n", 1)[1]
        if result.endswith("```"):
            result = result[:-3]
        result = result.strip()

        new_profile = json.loads(result)

        # Preserve metadata fields
        new_profile["user_name"] = key
        new_profile["total_interactions"] = profile.get("total_interactions", 0)
        new_profile["created_at"] = profile.get("created_at", datetime.now().isoformat())
        new_profile["last_updated"] = datetime.now().isoformat()

        _save_profile(key, new_profile)
        _interaction_counts[key] = 0  # Reset counter
        log.info(f"Profile updated for {user_name}")
        return True

    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"Profile update failed for {user_name}: {e}")
        return False


def get_style_prompt(user_name: str) -> str:
    """Get a compact style instruction for the system prompt (<500 tokens)."""
    profile = _load_profile(user_name)

    if profile.get("total_interactions", 0) < 3:
        return ""  # Not enough data yet

    parts = []

    # Response style
    style = profile.get("response_style", "balanced")
    if style == "concise":
        parts.append("Keep responses short and direct.")
    elif style == "detailed":
        parts.append("This person appreciates detailed explanations.")

    # Detail level
    detail = profile.get("detail_level", "moderate")
    if detail == "minimal":
        parts.append("Minimal detail — just the answer.")
    elif detail == "thorough":
        parts.append("Be thorough when explaining.")

    # Follow-up questions
    if profile.get("likes_followup_questions") is True:
        parts.append("Ask follow-up questions when natural.")
    elif profile.get("likes_followup_questions") is False:
        parts.append("Don't ask follow-up questions — just answer.")

    # Tone
    tone = profile.get("tone", "")
    if tone and tone != "match Frank's personality":
        parts.append(f"Tone: {tone}")

    # Topics
    topics = profile.get("topics_of_interest", [])
    if topics:
        parts.append(f"Interested in: {', '.join(topics[:4])}")

    # Communication notes
    notes = profile.get("communication_notes", [])
    for note in notes[:3]:
        parts.append(note)

    if not parts:
        return ""

    return "LEARNED STYLE FOR THIS USER:\n" + "\n".join(f"- {p}" for p in parts)


def format_profile(user_name: str) -> str:
    """Format a user's profile for display (!myprofile command)."""
    profile = _load_profile(user_name)
    if profile.get("total_interactions", 0) < 3:
        return f"Not enough data yet for {user_name}. I need a few more conversations to learn your style."

    lines = [f"STYLE PROFILE — {user_name.title()}\n"]
    lines.append(f"Total interactions: {profile.get('total_interactions', 0)}")
    lines.append(f"Response style: {profile.get('response_style', '?')}")
    lines.append(f"Detail level: {profile.get('detail_level', '?')}")
    lines.append(f"Likes follow-ups: {'yes' if profile.get('likes_followup_questions') else 'no'}")
    lines.append(f"Tone: {profile.get('tone', '?')}")

    topics = profile.get("topics_of_interest", [])
    if topics:
        lines.append(f"Topics: {', '.join(topics)}")

    notes = profile.get("communication_notes", [])
    if notes:
        lines.append(f"\nNotes:")
        for n in notes:
            lines.append(f"  - {n}")

    lines.append(f"\nLast updated: {profile.get('last_updated', 'never')}")
    return "\n".join(lines)


def reset_profile(user_name: str) -> str:
    """Reset a user's profile."""
    path = _profile_path(user_name)
    try:
        path.unlink()
        key = user_name.lower()
        _interaction_counts.pop(key, None)
        _recent_interactions.pop(key, None)
        return f"Profile reset for {user_name}. I'll start learning again from scratch."
    except FileNotFoundError:
        return f"No profile found for {user_name}."
