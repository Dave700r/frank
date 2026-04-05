"""Buddy — Tamagotchi-style companion pet that lives alongside Frank.
Inspired by the Buddy system in Claude Code architecture.

Each family member has their own Buddy that evolves based on interactions.
Species are determined by a deterministic gacha based on username hash.
Mood and energy change based on interaction frequency."""
import json
import hashlib
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("family-bot.buddy")

BUDDY_FILE = Path.home() / "family-bot" / "buddies.json"

# 18 species with rarity tiers
SPECIES = {
    # Common (60% chance)
    "cat": {"rarity": "common", "emoji": "🐱", "art": "(=^.^=)", "evolves_to": "lion"},
    "dog": {"rarity": "common", "emoji": "🐕", "art": "U・ᴥ・U", "evolves_to": "wolf"},
    "rabbit": {"rarity": "common", "emoji": "🐰", "art": "(\\(\\  /) )", "evolves_to": "jackalope"},
    "hamster": {"rarity": "common", "emoji": "🐹", "art": "@('.')@", "evolves_to": "capybara"},
    "frog": {"rarity": "common", "emoji": "🐸", "art": "( ° ͜ʖ °)", "evolves_to": "dragon_frog"},
    "bird": {"rarity": "common", "emoji": "🐦", "art": ">(')>", "evolves_to": "phoenix"},
    # Uncommon (25% chance)
    "fox": {"rarity": "uncommon", "emoji": "🦊", "art": "/\\  /\\{'>.'>}", "evolves_to": "nine_tails"},
    "owl": {"rarity": "uncommon", "emoji": "🦉", "art": "{O,O}", "evolves_to": "sage_owl"},
    "penguin": {"rarity": "uncommon", "emoji": "🐧", "art": "<(o )>", "evolves_to": "emperor"},
    "otter": {"rarity": "uncommon", "emoji": "🦦", "art": "~(°o°)~", "evolves_to": "sea_dragon"},
    "raccoon": {"rarity": "uncommon", "emoji": "🦝", "art": "[◕ᴥ◕]", "evolves_to": "tanuki"},
    # Rare (12% chance)
    "wolf": {"rarity": "rare", "emoji": "🐺", "art": "/\\{°w°}/\\", "evolves_to": "fenrir"},
    "lion": {"rarity": "rare", "emoji": "🦁", "art": ">{=◕ᆺ◕=}<", "evolves_to": "sphinx"},
    "phoenix": {"rarity": "rare", "emoji": "🔥", "art": "~{^v^}~", "evolves_to": "solar_phoenix"},
    # Legendary (3% chance)
    "dragon": {"rarity": "legendary", "emoji": "🐉", "art": "/\\_/\\{>.<}", "evolves_to": None},
    "unicorn": {"rarity": "legendary", "emoji": "🦄", "art": "/>{'*.~}", "evolves_to": None},
    "kraken": {"rarity": "legendary", "emoji": "🐙", "art": "C{°.°}C", "evolves_to": None},
    "yeti": {"rarity": "legendary", "emoji": "❄️", "art": "/[°A°]\\", "evolves_to": None},
}

MOODS = {
    "ecstatic": {"emoji": "✨", "decay_hours": 2},
    "happy": {"emoji": "😊", "decay_hours": 6},
    "content": {"emoji": "😌", "decay_hours": 12},
    "bored": {"emoji": "😐", "decay_hours": 24},
    "lonely": {"emoji": "😢", "decay_hours": 48},
    "sleepy": {"emoji": "😴", "decay_hours": 72},
}

EVOLUTION_THRESHOLD = 50  # Interactions needed to evolve


def _load_buddies() -> dict:
    try:
        with open(BUDDY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_buddies(buddies: dict):
    with open(BUDDY_FILE, "w") as f:
        json.dump(buddies, f, indent=2)


def _pick_species(username: str) -> str:
    """Deterministic gacha — same username always gets same species."""
    h = int(hashlib.sha256(username.encode()).hexdigest(), 16)
    roll = h % 100

    if roll < 3:  # 3% legendary
        pool = [s for s, d in SPECIES.items() if d["rarity"] == "legendary"]
    elif roll < 15:  # 12% rare
        pool = [s for s, d in SPECIES.items() if d["rarity"] == "rare"]
    elif roll < 40:  # 25% uncommon
        pool = [s for s, d in SPECIES.items() if d["rarity"] == "uncommon"]
    else:  # 60% common
        pool = [s for s, d in SPECIES.items() if d["rarity"] == "common"]

    return pool[h % len(pool)]


def _get_mood(buddy: dict) -> str:
    """Calculate current mood based on last interaction time."""
    last = buddy.get("last_interaction")
    if not last:
        return "lonely"

    hours_since = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600

    if hours_since < 1:
        return "ecstatic"
    elif hours_since < 4:
        return "happy"
    elif hours_since < 12:
        return "content"
    elif hours_since < 24:
        return "bored"
    elif hours_since < 48:
        return "lonely"
    else:
        return "sleepy"


def get_or_create_buddy(username: str) -> dict:
    """Get or create a buddy for a user."""
    buddies = _load_buddies()
    key = username.lower()

    if key not in buddies:
        species = _pick_species(key)
        buddies[key] = {
            "species": species,
            "name": None,  # User can name their buddy
            "level": 1,
            "interactions": 0,
            "created_at": datetime.now().isoformat(),
            "last_interaction": datetime.now().isoformat(),
            "evolved": False,
        }
        _save_buddies(buddies)
        log.info(f"New buddy for {username}: {species} ({SPECIES[species]['rarity']})")

    return buddies[key]


def interact(username: str) -> dict:
    """Record an interaction. Called after every AI conversation.
    Returns buddy state update if anything interesting happened."""
    buddies = _load_buddies()
    key = username.lower()

    if key not in buddies:
        get_or_create_buddy(username)
        buddies = _load_buddies()

    buddy = buddies[key]
    old_mood = _get_mood(buddy)

    buddy["interactions"] += 1
    buddy["last_interaction"] = datetime.now().isoformat()

    result = {"leveled_up": False, "evolved": False, "mood_change": None}

    # Level up every 10 interactions
    new_level = (buddy["interactions"] // 10) + 1
    if new_level > buddy["level"]:
        buddy["level"] = new_level
        result["leveled_up"] = True

    # Evolution check
    species_data = SPECIES.get(buddy["species"], {})
    if (not buddy["evolved"] and
            buddy["interactions"] >= EVOLUTION_THRESHOLD and
            species_data.get("evolves_to")):
        buddy["evolved"] = True
        buddy["species"] = species_data["evolves_to"]
        # Add evolved species to SPECIES if not there (use parent data)
        if buddy["species"] not in SPECIES:
            SPECIES[buddy["species"]] = {
                "rarity": "evolved",
                "emoji": "⭐",
                "art": species_data["art"].replace(".", "*"),
                "evolves_to": None,
            }
        result["evolved"] = True

    new_mood = _get_mood(buddy)
    if old_mood != new_mood:
        result["mood_change"] = (old_mood, new_mood)

    buddies[key] = buddy
    _save_buddies(buddies)

    return result


def format_buddy(username: str) -> str:
    """Format buddy info for display."""
    buddy = get_or_create_buddy(username)
    species = buddy["species"]
    data = SPECIES.get(species, {"emoji": "❓", "art": "(?)", "rarity": "unknown"})
    mood = _get_mood(buddy)
    mood_data = MOODS.get(mood, {"emoji": "❓"})

    name = buddy.get("name") or species.replace("_", " ").title()
    rarity = data["rarity"].upper()

    lines = [
        f"{data['emoji']} {name} — {rarity}",
        f"  {data['art']}",
        f"  Level {buddy['level']} | {mood_data['emoji']} {mood}",
        f"  Interactions: {buddy['interactions']}",
    ]

    if not buddy["evolved"] and SPECIES.get(species, {}).get("evolves_to"):
        remaining = EVOLUTION_THRESHOLD - buddy["interactions"]
        if remaining > 0:
            lines.append(f"  Evolves in {remaining} more interactions")
        else:
            lines.append(f"  Ready to evolve!")
    elif buddy["evolved"]:
        lines.append(f"  ⭐ EVOLVED")

    return "\n".join(lines)


def name_buddy(username: str, name: str) -> str:
    """Name or rename a buddy."""
    buddies = _load_buddies()
    key = username.lower()
    if key not in buddies:
        return "You don't have a buddy yet! Talk to me first."

    buddies[key]["name"] = name
    _save_buddies(buddies)
    return f"Your buddy is now named {name}!"


def get_interaction_message(result: dict, username: str) -> str:
    """Generate a message about buddy events (level up, evolution, etc).
    Returns empty string if nothing notable happened."""
    buddy = get_or_create_buddy(username)
    species = buddy["species"]
    data = SPECIES.get(species, {"emoji": "❓"})
    name = buddy.get("name") or species.replace("_", " ").title()

    parts = []

    if result.get("evolved"):
        parts.append(f"\n⭐ {name} EVOLVED into {species.replace('_', ' ').title()}! ⭐")

    if result.get("leveled_up"):
        parts.append(f"\n{data['emoji']} {name} reached level {buddy['level']}!")

    return "\n".join(parts)
