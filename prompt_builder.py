"""Modular system prompt builder for Frank.
Splits prompts into static (cached) and dynamic (per-request) sections.
Saves tokens by keeping the static personality/rules consistent."""
import logging
from datetime import datetime

import config
import humanize

log = logging.getLogger("family-bot.prompts")

# Static sections — cached, don't change per request
_static_cache = None


def _build_static():
    """Build the static portion of the system prompt. Cached after first call."""
    global _static_cache
    if _static_cache:
        return _static_cache

    from frank_persona import FRANK_CHARACTER
    _static_cache = FRANK_CHARACTER
    return _static_cache


def build_system_prompt(user_name: str, is_private: bool, context: str,
                        memories: str, recipe_context: str, search_context: str,
                        email_context: str, finance_context: str) -> str:
    """Build the full system prompt from static + dynamic sections."""
    from frank_persona import CAPABILITIES_PROMPT

    static = _build_static()

    # Dynamic: time-of-day personality
    time_mod = humanize.get_time_personality()

    # Dynamic: privacy context
    owner_nick = config.FAMILY_MEMBERS[config.OWNER]["nickname"]
    email_privacy = (
        f"This is a PRIVATE chat with {owner_nick}. You can freely discuss email content, "
        "bills, finances, and personal info here."
        if is_private else
        "This is a GROUP chat. Email and financial details are private — "
        f"tell {owner_nick} you'll DM them instead."
    )

    capabilities = CAPABILITIES_PROMPT.format(
        context=context,
        memories=memories,
        recipe_context=recipe_context,
        search_context=search_context,
        email_privacy=email_privacy,
        email_context=email_context,
        finance_context=finance_context,
        user_name=user_name or "a family member",
        chat_type="private DM" if is_private else "family group chat",
    )

    # User-specific style adaptation
    import style_learner
    style_notes = style_learner.get_style_prompt(user_name)

    prompt = f"{static}\n\n{capabilities}\n\nCURRENT VIBE: {time_mod}"
    if style_notes:
        prompt += f"\n\n{style_notes}"
    return prompt


def invalidate_cache():
    """Call if Frank's personality files change at runtime."""
    global _static_cache
    _static_cache = None
