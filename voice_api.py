"""Lightweight HTTP API for the voice assistant to call.
Runs alongside the Telegram bot on port 5123 (localhost only).
"""
import json
import logging
from aiohttp import web
import ai
import db
import firefly
import conversation_log
import mem0_memory
import reminders
import config
from datetime import datetime, timedelta

log = logging.getLogger("family-bot.voice-api")

async def handle_query(request):
    """POST /query {"text": "...", "user": "owner_name"}
    Returns {"reply": "..."}"""
    try:
        data = await request.json()
        text = data.get("text", "").strip()
        user = data.get("user", config.OWNER)

        if not text:
            return web.json_response({"reply": "I didn't catch that."})

        # Check for quick actions first (no AI needed)
        lower = text.lower()

        if any(w in lower for w in ("grocery list", "shopping list", "what do we need")):
            items = db.get_shopping_list()
            if not items:
                return web.json_response({"reply": "Shopping list is empty, we're all good!"})
            names = [item["name"] for item in items]
            reply = f"We need {len(names)} things: {', '.join(names)}"
            return web.json_response({"reply": reply})

        if lower.startswith("add "):
            item = text[4:].strip()
            if item:
                db.add_shopping_item(item, requested_by=user)
                return web.json_response({"reply": f"Added {item} to the list."})

        if lower.startswith(("bought ", "got ")):
            item = text.split(" ", 1)[1].strip()
            if item and db.mark_item_bought(item, bought_by=user):
                return web.json_response({"reply": f"Marked {item} as bought."})

        # AI for everything else
        result = ai.handle_message(text, user_name=user)
        reply = result["reply"]

        # Execute actions if AI decided on any
        for action in result.get("actions", []):
            act = action.get("action")
            item = action.get("item", "")
            if act == "add" and item:
                db.add_shopping_item(item, requested_by=user)
            elif act == "bought" and item:
                db.mark_item_bought(item, bought_by=user)
            elif act == "remove" and item:
                db.remove_shopping_item(item)
            elif act == "remind":
                remind_text = action.get("time", "") + " " + action.get("message", "")
                remind_at, remind_msg = reminders.parse_reminder_time(remind_text)
                telegram_id = config.FAMILY_MEMBERS.get(user, {}).get("telegram_id", "")
                if remind_at and remind_msg and telegram_id:
                    reminders.add_reminder(user, telegram_id, remind_msg, remind_at)
                elif action.get("message") and telegram_id:
                    reminders.add_reminder(user, telegram_id, action["message"],
                                          datetime.now() + timedelta(hours=1))
            elif act == "log_spend":
                store = action.get("store", "Unknown")
                amount = action.get("amount", 0)
                if amount:
                    db.log_spend(store, float(amount))
                    try:
                        firefly.log_receipt(store, float(amount))
                    except Exception:
                        pass

        # Log to conversation memory (both systems)
        conversation_log.log_interaction(user, text, reply)
        conversation_log.extract_and_save_learnings(user, text, reply)
        mem0_memory.add_conversation(user, text, reply)

        return web.json_response({"reply": reply})

    except Exception as e:
        log.error(f"Voice API error: {e}")
        return web.json_response({"reply": "Sorry, something went wrong."}, status=500)


async def handle_matrix_alert(request):
    """POST /matrix-alert {"text": "...", "room_id": "optional"} — send alert to Matrix room."""
    try:
        import matrix_client
        data = await request.json()
        text = data.get("text", "").strip()
        room_id = data.get("room_id", "")
        image_b64 = data.get("image_b64", "")
        if not text:
            return web.json_response({"status": "error", "message": "No text"}, status=400)
        await matrix_client.send_alert(text, room_id=room_id or None, image_b64=image_b64 or None)
        return web.json_response({"status": "ok"})
    except Exception as e:
        log.error(f"Matrix alert error: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


def create_app():
    app = web.Application()
    app.router.add_post("/query", handle_query)
    app.router.add_post("/matrix-alert", handle_matrix_alert)
    return app


async def start_voice_api():
    """Start the voice API server as a background task."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 5123)
    await site.start()
    log.info("Voice API listening on http://127.0.0.1:5123")
