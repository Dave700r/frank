"""ULTRAPLAN — Extended thinking for complex requests.
When Frank detects a request that needs deep analysis, he defers to a
longer planning session with more tokens and a planning-specific prompt.

Triggers on complex questions about finances, meal planning, schedules,
or anything that needs multi-step reasoning."""
import logging
import os
import httpx
import config

log = logging.getLogger("family-bot.ultraplan")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
# Use a stronger model for planning if available, otherwise same model with more tokens
PLAN_MODEL = "google/gemini-2.0-flash-001"
PLAN_MAX_TOKENS = 1500  # 5x normal

PLAN_SYSTEM = """You are Frank's planning brain. When the family needs help thinking through
something complex, you take your time and think it through carefully.

Your job:
1. Break down the problem into steps
2. Consider relevant context (family preferences, budget, schedule)
3. Provide a clear, actionable plan
4. Anticipate follow-up questions

Be thorough but practical. This family doesn't need corporate strategy docs —
they need clear answers they can act on. Use bullet points and short sections.

Keep Frank's voice — direct, warm, no fluff."""


PLAN_TRIGGERS = [
    "plan", "help me figure out", "how should we", "what's the best way to",
    "budget", "meal plan", "weekly plan", "schedule", "organize",
    "compare", "pros and cons", "should we", "help me decide",
    "what would you recommend", "think through", "figure out",
    "strategy", "approach", "how do we handle",
]


def should_ultraplan(text: str) -> bool:
    """Detect if a message needs extended thinking."""
    lower = text.lower()
    # Must be a substantial request (not just "plan")
    if len(text.split()) < 5:
        return False
    return any(trigger in lower for trigger in PLAN_TRIGGERS)


def run_plan(text: str, context: str = "", user_name: str = None) -> str:
    if user_name is None:
        user_name = config.OWNER
    """Execute an ULTRAPLAN — longer thinking with more tokens."""
    log.info(f"ULTRAPLAN triggered for {user_name}: {text[:50]}...")

    system = PLAN_SYSTEM
    if context:
        system += f"\n\nCONTEXT:\n{context}"

    try:
        resp = httpx.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                "Content-Type": "application/json",
            },
            json={
                "model": PLAN_MODEL,
                "max_tokens": PLAN_MAX_TOKENS,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
            },
            timeout=90,  # Longer timeout for planning
        )
        resp.raise_for_status()
        data = resp.json()

        # Track tokens
        try:
            from pathlib import Path
            import sys
            sys.path.insert(0, str(Path.home() / "gatekeeper"))
            import token_tracker
            usage = data.get("usage", {})
            if usage:
                token_tracker.log_usage(
                    bot="frank-ultraplan",
                    model=PLAN_MODEL,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    context="ultraplan",
                )
        except Exception:
            pass

        result = data["choices"][0]["message"]["content"]
        log.info(f"ULTRAPLAN complete: {len(result)} chars")
        return result

    except Exception as e:
        log.error(f"ULTRAPLAN failed: {e}")
        return None
