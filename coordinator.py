"""Multi-agent coordinator for Frank.
Spawns parallel sub-tasks for complex requests that need multiple data sources.
Inspired by the multi-agent coordinator pattern from Claude Code architecture.

Example: "Give me a full status update" triggers parallel:
  - Weather check
  - Grocery inventory check
  - Finance summary
  - Email check
  - Calendar/reminders check
Results are combined into one coherent response."""
import asyncio
import logging
import time
from datetime import datetime

log = logging.getLogger("family-bot.coordinator")


class SubTask:
    def __init__(self, name: str, fn, timeout: float = 15.0):
        self.name = name
        self.fn = fn  # async or sync callable returning a string
        self.timeout = timeout
        self.result = None
        self.error = None
        self.elapsed = 0.0


async def _run_subtask(task: SubTask):
    """Run a single subtask with timeout."""
    start = time.time()
    try:
        if asyncio.iscoroutinefunction(task.fn):
            task.result = await asyncio.wait_for(task.fn(), timeout=task.timeout)
        else:
            task.result = await asyncio.get_event_loop().run_in_executor(
                None, task.fn
            )
    except asyncio.TimeoutError:
        task.error = f"timed out ({task.timeout}s)"
    except Exception as e:
        task.error = str(e)
    task.elapsed = time.time() - start


async def run_parallel(tasks: list[SubTask]) -> dict:
    """Run multiple subtasks in parallel, collect results.
    Returns dict of {task_name: result_or_error}."""
    await asyncio.gather(*[_run_subtask(t) for t in tasks])

    results = {}
    for t in tasks:
        if t.result is not None:
            results[t.name] = t.result
        elif t.error:
            results[t.name] = f"[error: {t.error}]"
        else:
            results[t.name] = "[no data]"

    total_time = max(t.elapsed for t in tasks) if tasks else 0
    log.info(f"Parallel tasks completed: {len(tasks)} tasks in {total_time:.1f}s "
             f"(vs {sum(t.elapsed for t in tasks):.1f}s sequential)")

    return results


def build_combined_context(results: dict) -> str:
    """Combine parallel task results into a single context string for the AI."""
    parts = []
    for name, result in results.items():
        parts.append(f"--- {name.upper()} ---\n{result}")
    return "\n\n".join(parts)


# ─── Pre-built task sets for common complex requests ───

def get_full_status_tasks() -> list[SubTask]:
    """Tasks for a comprehensive status update."""
    import db
    import firefly
    import email_client
    import reminders as rem_module
    import briefing

    def check_groceries():
        items = db.get_shopping_list()
        if not items:
            return "Shopping list is empty."
        names = [i["name"] for i in items]
        return f"{len(items)} items on the list: {', '.join(names)}"

    def check_inventory():
        low = db.get_low_stock_items()
        if not low:
            return "All stock levels OK."
        return f"Low stock: {', '.join(i['name'] for i in low)}"

    def check_finances():
        try:
            summary = firefly.get_monthly_summary()
            return f"Spending this month: ${summary['total']:.2f}"
        except Exception:
            return "Firefly unavailable"

    def check_email():
        try:
            count = email_client.get_unread_count()
            return f"{count} unread emails"
        except Exception:
            return "Email unavailable"

    def check_reminders():
        import config
        pending = rem_module.get_pending_for_user(config.OWNER)
        if not pending:
            return "No pending reminders."
        return f"{len(pending)} pending reminders"

    def check_weather():
        try:
            return briefing.get_weather_summary()
        except Exception:
            return "Weather unavailable"

    return [
        SubTask("groceries", check_groceries, timeout=5),
        SubTask("inventory", check_inventory, timeout=5),
        SubTask("finances", check_finances, timeout=10),
        SubTask("email", check_email, timeout=10),
        SubTask("reminders", check_reminders, timeout=5),
        SubTask("weather", check_weather, timeout=10),
    ]


def get_morning_tasks() -> list[SubTask]:
    """Tasks for building a morning briefing in parallel."""
    return get_full_status_tasks()  # Same set for now


# ─── Detection: should we use parallel tasks? ───

PARALLEL_TRIGGERS = [
    "full status", "full update", "what's going on", "catch me up",
    "give me everything", "what did i miss", "status update",
    "how's everything", "morning update", "what's happening",
    "overview", "sitrep",
]


def should_use_parallel(text: str) -> bool:
    """Detect if a message warrants parallel sub-task execution."""
    lower = text.lower()
    return any(trigger in lower for trigger in PARALLEL_TRIGGERS)
