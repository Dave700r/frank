"""Human-like behavior utilities for Frank.
Typing delays, message batching, engagement scoring, and response chunking."""
import asyncio
import random
import logging
import time
from collections import defaultdict

log = logging.getLogger("family-bot.humanize")

# Track recent activity per chat for engagement scoring
_last_participated = {}  # chat_id -> timestamp

# ─── Group Chat Engagement Scoring ───

FRANK_TOPICS = {
    "grocery", "groceries", "shopping", "list", "buy", "bought", "store",
    "recipe", "cook", "dinner", "lunch", "breakfast", "food", "eat",
    "weather", "forecast", "rain", "snow",
    "remind", "reminder", "schedule", "appointment",
    "spend", "spent", "money", "budget", "bill", "balance", "payment",
    "email", "inbox", "mail",
    "stock", "inventory", "running low", "out of",
    "cistern", "water",
    "frank",
}


def should_respond_in_group(text: str, chat_id: str, is_private: bool) -> tuple:
    """Decide whether Frank should respond in a group chat.
    Returns (should_respond: bool, score: float).
    Always responds in DMs."""
    if is_private:
        return True, 1.0

    lower = text.lower()

    # Always respond to direct mentions
    if "frank" in lower:
        return True, 1.0

    score = 0.0

    # Question mark — someone asking something
    if text.strip().endswith("?"):
        score += 0.35

    # Frank's domain topics
    matched_topics = sum(1 for t in FRANK_TOPICS if t in lower)
    score += min(matched_topics * 0.2, 0.5)

    # Recently participated in this chat (last 5 minutes)
    last = _last_participated.get(chat_id, 0)
    if time.time() - last < 300:
        score += 0.25

    # Message is long enough to be substantive (not just "lol" that passed ack filter)
    if len(text.split()) >= 5:
        score += 0.1

    # Small random chance to chime in naturally
    score += random.uniform(0, 0.1)

    should = score >= 0.45
    if should:
        log.debug(f"Engagement score {score:.2f} for group message — responding")
    else:
        log.debug(f"Engagement score {score:.2f} for group message — staying quiet")

    return should, score


def mark_participated(chat_id: str):
    """Record that Frank participated in a chat (for engagement scoring)."""
    _last_participated[chat_id] = time.time()


# ─── Typing Delay ───

async def human_delay(message: str, response: str):
    """Simulate human reading + thinking + typing time."""
    # Read the incoming message (~150ms per word)
    read_time = len(message.split()) * 0.15
    # Think (~0.5-2s)
    think_time = random.uniform(0.5, 2.0)
    # Type the response (~60ms per char, capped)
    type_time = min(len(response) * 0.06, 6.0)
    # Total with jitter
    delay = read_time + think_time + type_time + random.uniform(0.2, 0.8)
    # Cap between 1-10 seconds
    delay = max(1.0, min(delay, 10.0))
    await asyncio.sleep(delay)


def should_chunk(response: str) -> bool:
    """Decide if a response should be split into multiple messages."""
    return len(response) > 400 and "\n\n" in response


def chunk_response(response: str) -> list:
    """Split a long response into natural chunks."""
    if not should_chunk(response):
        return [response]

    chunks = []
    for part in response.split("\n\n"):
        part = part.strip()
        if not part:
            continue
        # If the last chunk is short, merge with this one
        if chunks and len(chunks[-1]) < 150:
            chunks[-1] += "\n\n" + part
        else:
            chunks.append(part)

    return chunks if chunks else [response]


# ─── Message Batching ───

class MessageBatcher:
    """Wait for rapid-fire messages before responding.
    If a user sends multiple messages quickly, combine them into one context."""

    def __init__(self, delay: float = 2.5):
        self.delay = delay
        self._pending = {}  # chat_id -> {"messages": [...], "task": Task, "callback": fn}

    async def add(self, chat_id: str, message: str, user_name: str,
                  callback, **kwargs):
        """Add a message. If no more messages arrive within delay, fires callback."""
        key = str(chat_id)

        if key in self._pending:
            # Cancel the previous timer, add to batch
            self._pending[key]["messages"].append(message)
            self._pending[key]["task"].cancel()
        else:
            self._pending[key] = {
                "messages": [message],
                "callback": callback,
                "user_name": user_name,
                "kwargs": kwargs,
            }

        # Set new timer
        self._pending[key]["task"] = asyncio.create_task(
            self._flush(key)
        )

    async def _flush(self, key: str):
        """Wait for the batch delay, then process all collected messages."""
        await asyncio.sleep(self.delay)

        if key not in self._pending:
            return

        data = self._pending.pop(key)
        messages = data["messages"]
        callback = data["callback"]
        user_name = data["user_name"]
        kwargs = data["kwargs"]

        # Combine messages
        if len(messages) > 1:
            combined = "\n".join(messages)
            log.info(f"Batched {len(messages)} messages from {user_name}")
        else:
            combined = messages[0]

        try:
            await callback(combined, user_name=user_name, **kwargs)
        except Exception as e:
            log.error(f"Batch callback error: {e}")


# ─── In-Character Error Messages ───

ERROR_RESPONSES = [
    "hmm, lost my train of thought there",
    "brain froze for a sec. what were you saying?",
    "ugh, something's off. try me again?",
    "my bad, got confused. give me another shot",
    "hold on, something hiccupped. try again?",
    "well that didn't work. hit me again",
]

def get_error_response() -> str:
    """Get a random in-character error message."""
    return random.choice(ERROR_RESPONSES)


# ─── Natural Response Length ───

def get_max_tokens(message: str) -> int:
    """Match response length to input length — short input, short output."""
    words = len(message.split())
    if words < 4:
        return random.randint(30, 80)
    elif words < 15:
        return random.randint(60, 200)
    elif words < 40:
        return random.randint(150, 350)
    else:
        return random.randint(200, 500)


# ─── Time-of-Day Energy ───

def get_time_personality() -> str:
    """Get a time-of-day personality modifier for the system prompt."""
    from datetime import datetime
    hour = datetime.now().hour
    if 6 <= hour < 10:
        return "You're a bit groggy, still warming up. Keep responses shorter."
    elif 10 <= hour < 14:
        return "You're at peak energy. Engaged, a bit chattier than usual."
    elif 14 <= hour < 18:
        return "Steady afternoon energy. Normal Frank."
    elif 18 <= hour < 22:
        return "Winding down for the evening. More relaxed, reflective."
    else:
        return "It's late. You're sleepy. Keep it very brief."
