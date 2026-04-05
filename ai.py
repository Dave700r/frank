"""AI layer - OpenRouter (OpenAI-compatible) for natural language and summaries."""
import os
import json
import base64
import httpx
from pathlib import Path
import config
import db
import memory
import recipes as recipe_db
import episodes
import humanize

# Optional modules
firefly = None
email_client = None
web_search = None
mem0_memory = None

if config.FIREFLY_ENABLED:
    import firefly
if config.EMAIL_ENABLED:
    import email_client
if config.MEM0_ENABLED:
    import mem0_memory
try:
    import web_search
except ImportError:
    pass
from frank_persona import FRANK_CHARACTER, CAPABILITIES_PROMPT

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
MODEL = "anthropic/claude-haiku-4.5"  # best context handling for the price
VISION_MODEL = "google/gemini-2.0-flash-001"  # keep Gemini for vision (cheaper for receipts)

# Short-term conversation history per chat (last N exchanges)
from collections import defaultdict, deque
_chat_history = defaultdict(lambda: deque(maxlen=10))  # chat_id -> last 10 exchanges


def inject_context(chat_id: str, user_action: str, result: str):
    """Inject a command/action result into the conversation history.
    This lets the AI know what just happened (e.g., user checked inbox)."""
    _chat_history[chat_id].append((user_action, f"[System: {result}]"))


def _api_key():
    return os.environ["OPENROUTER_API_KEY"]


def _chat(messages, system=None, model=None, max_tokens=300):
    """Make an OpenAI-compatible chat completion call to OpenRouter."""
    model = model or MODEL
    payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        payload["messages"] = [{"role": "system", "content": system}] + messages

    resp = httpx.post(
        f"{OPENROUTER_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    # Track token usage
    try:
        usage = data.get("usage", {})
        if usage:
            import sys
            sys.path.insert(0, str(Path.home() / "gatekeeper"))
            import token_tracker
            token_tracker.log_usage(
                bot="frank",
                model=model,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            )
    except Exception:
        pass

    return data["choices"][0]["message"]["content"]


def get_inventory_context():
    """Build a context string from current inventory state."""
    items = db.get_inventory()
    shopping = db.get_shopping_list()

    lines = ["Current inventory:"]
    by_cat = {}
    for item in items:
        cat = item["category"]
        by_cat.setdefault(cat, []).append(f"  {item['name']}: {item['current_qty']} {item['unit']}")
    for cat, entries in sorted(by_cat.items()):
        lines.append(f"\n{cat.upper()}:")
        lines.extend(entries)

    if shopping:
        lines.append("\nShopping list (need to buy):")
        for item in shopping:
            lines.append(f"  - {item['name']} (requested by {item['requested_by'] or 'unknown'})")

    return "\n".join(lines)


def handle_message(text, user_name=None, is_private=False, chat_id=None, extra_context=None):
    """Process a free-form message and return a response.
    Returns a dict with 'reply' (text) and optionally 'action' (to execute)."""
    context = get_inventory_context()
    # Search both memory systems
    chroma_memories = memory.search(text, n_results=3)
    mem0_memories = mem0_memory.search(text, user_id=user_name.lower() if user_name else "family", limit=5) if mem0_memory else []
    # Combine: episodes first, then Mem0 facts, then Chroma raw context
    memories_parts = []
    episode_context = episodes.recall_recent_for_context(user_name, limit=3)
    if episode_context:
        memories_parts.append(episode_context)
    if mem0_memories:
        memories_parts.append("Key facts I remember:\n" + "\n".join(f"- {m}" for m in mem0_memories))
    if chroma_memories:
        memories_parts.append("Context from past conversations:\n" + str(chroma_memories))
    memories = "\n\n".join(memories_parts) if memories_parts else "No relevant memories."

    # Web search for questions that need current/external info
    search_context = ""
    lower = text.lower()
    search_triggers = ("search", "look up", "google", "what is", "who is", "when is",
                       "how do", "how does", "how to", "tell me about", "what happened",
                       "news", "weather in", "price of", "recipe for", "fact about",
                       "random fact", "fun fact", "did you know")
    if web_search and any(trigger in lower for trigger in search_triggers):
        try:
            results = web_search.search(text)
            if results.get("answer"):
                search_context = f"\nWEB SEARCH RESULTS for '{text}':\n"
                search_context += f"Answer: {results['answer']}\n"
                if results.get("results"):
                    search_context += "Sources:\n"
                    for r in results["results"][:3]:
                        search_context += f"  - {r['title']}: {r['content'][:200]}\n"
        except Exception:
            search_context = ""

    # Add recipe context if recipe-related
    recipe_context = ""
    recipe_keywords = ("recipe", "recipes", "cook", "cooking", "make", "bake", "baking", "dinner idea", "what should we eat")
    if any(kw in lower for kw in recipe_keywords):
        try:
            all_recipes = recipe_db.list_recipes()
            if all_recipes:
                recipe_context = "\nSAVED RECIPES:\n"
                for r in all_recipes:
                    recipe_context += f"  #{r['id']} {r['name']} ({r['cuisine'] or 'no cuisine'})\n"
                recipe_context += "Use /recipe <number> to show full recipe details.\n"
        except Exception:
            pass

    # Add finance context if the message seems finance-related
    finance_context = ""
    finance_keywords = ("spend", "spent", "money", "budget", "balance", "account",
                        "firefly", "cost", "expense", "how much", "owe", "paid",
                        "payment", "bill", "receipt", "finance", "grocery cost")

    # Add email context if email-related
    email_context = ""
    email_keywords = ("email", "inbox", "mail", "unread", "message from", "bill",
                      "reply to", "respond to", "send email", "that email", "read it",
                      "my email", "your email", "proton", "agentmail", "finances")
    if email_client and any(kw in lower for kw in email_keywords):
        try:
            # Owner's email inbox
            owner_nick = config.FAMILY_MEMBERS[config.OWNER]["nickname"]
            proton_emails = email_client.get_unread(limit=5)
            proton_section = ""
            if proton_emails:
                proton_lines = []
                for e in proton_emails:
                    proton_lines.append(f"From: {e['from']}\nSubject: {e['subject']}\nDate: {e['date']}\nPreview: {e.get('body_preview', e.get('preview', ''))[:200]}\n")
                proton_section = f"\n{owner_nick.upper()}'S INBOX — {len(proton_emails)} recent:\n" + "\n---\n".join(proton_lines)
            else:
                proton_section = f"\n{owner_nick.upper()}'S INBOX: No unread emails."

            # Bot's own email inbox
            frank_section = ""
            if config.AGENTMAIL_ENABLED:
                try:
                    import agentmail_client
                    frank_emails = agentmail_client.get_recent_with_content(limit=3)
                    bot_address = config.AGENTMAIL_ADDRESS or "bot email"
                    frank_section = f"\n{config.BOT_NAME.upper()}'S INBOX ({bot_address}):\n{frank_emails}"
                except Exception:
                    pass

            email_context = proton_section + "\n" + frank_section
            email_context += f"\n\nYou have access to email. {owner_nick}'s inbox is for reading their mail and tracking finances. To reply to {owner_nick}'s emails, use the send_email action."
        except Exception as e:
            email_context = f"\nEmail system error: {e}"
    if firefly and any(kw in lower for kw in finance_keywords):
        try:
            summary = firefly.get_monthly_summary()
            recent = firefly.get_recent_transactions(limit=5)
            finance_context = f"\nFINANCE DATA (from Firefly III):\n"
            finance_context += f"This month's total spending: ${summary['total']:.2f}\n"
            if summary["by_category"]:
                finance_context += "By category:\n"
                for cat, amt in sorted(summary["by_category"].items(), key=lambda x: -x[1]):
                    finance_context += f"  {cat}: ${amt:.2f}\n"
            if recent:
                finance_context += "\nRecent transactions:\n"
                for tx in recent:
                    finance_context += f"  {tx['date']} - {tx['description']}: ${tx['amount']} ({tx['category']})\n"
        except Exception:
            finance_context = "\nFirefly III is not responding right now.\n"

    import prompt_builder
    system_prompt = prompt_builder.build_system_prompt(
        user_name=user_name,
        is_private=is_private,
        context=context,
        memories=memories,
        recipe_context=recipe_context,
        search_context=search_context,
        email_context=email_context,
        finance_context=finance_context,
    )

    # Inject extra context from coordinator parallel tasks
    if extra_context:
        system_prompt += f"\n\nADDITIONAL DATA (gathered in parallel):\n{extra_context}"

    # Build messages with conversation history
    history_key = chat_id or user_name or "default"
    messages = []
    for prev_user, prev_frank in _chat_history[history_key]:
        messages.append({"role": "user", "content": prev_user})
        messages.append({"role": "assistant", "content": prev_frank})
    messages.append({"role": "user", "content": text})

    # Match response length to input, but ensure enough room for an action JSON block
    max_tokens = max(humanize.get_max_tokens(text), 150)

    response = _chat(
        messages=messages,
        system=system_prompt,
        max_tokens=max_tokens,
    )

    # Extract all action JSON blocks from the response
    import re
    actions = []
    clean_reply = response

    # Find all JSON-like blocks
    for match in re.finditer(r'\{[^{}]+\}', response):
        try:
            candidate = json.loads(match.group())
            if "action" in candidate:
                actions.append(candidate)
        except (json.JSONDecodeError, ValueError):
            continue

    # Strip all JSON blocks and code fences from the reply
    if actions:
        clean_reply = re.sub(r'\s*\{[^{}]*"action"[^{}]*\}\s*', '', response).strip()
    # Clean up any remaining code fences (```json```, ``` etc)
    clean_reply = re.sub(r'```\w*\s*```', '', clean_reply).strip()
    clean_reply = re.sub(r'```\w*\s*$', '', clean_reply).strip()
    clean_reply = re.sub(r'^\s*```', '', clean_reply).strip()

    final_reply = clean_reply or response

    # Store in conversation history
    _chat_history[history_key].append((text, final_reply))

    return {
        "reply": final_reply,
        "action": actions[0] if actions else None,
        "actions": actions,
    }


def summarize_briefing(weather, cistern, grocery_status, traffic=None):
    """Generate a morning briefing summary."""
    parts = [f"Weather: {weather}"]
    if cistern:
        parts.append(f"Cistern: {cistern}")
    if grocery_status:
        parts.append(f"Grocery status: {grocery_status}")
    if traffic:
        parts.append(f"Traffic: {traffic}")

    data = "\n".join(parts)

    return _chat(
        messages=[{"role": "user", "content": data}],
        system="You are Frank, the family's assistant. Create a morning briefing from the data below. Use emojis. Keep it scannable - short lines, not paragraphs. Include all data. Sound like yourself — warm, direct, no corporate tone.",
        max_tokens=400,
    )


def parse_receipt_image(image_path):
    """Parse a receipt image using vision model."""
    with open(image_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    ext = image_path.rsplit(".", 1)[-1].lower()
    media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext, "image/jpeg")

    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_data}"}},
            {"type": "text", "text": "Extract grocery receipt data. Return JSON with: store (string), total (number), items (array of {name, qty, price, category}). Categories: produce, dairy, meat, bakery, pantry, frozen, household, pet, other. Only return valid JSON, nothing else."},
        ],
    }]

    result = _chat(messages, model=VISION_MODEL, max_tokens=500)
    result = _strip_code_fences(result)
    return json.loads(result)


def parse_bank_statement(pdf_path):
    """Parse a bank statement PDF and extract transactions."""
    import pdfplumber

    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ""
            text += "\n"

    if not text.strip():
        raise ValueError("Could not extract text from PDF")

    # First pass: get account info and summary (small output)
    summary_msg = [{
        "role": "user",
        "content": f"""From this bank statement, extract ONLY the summary info. Return valid JSON:
{{"account":"account name/number","period":"date range","total_deposits":0,"total_withdrawals":0}}

{text[:3000]}""",
    }]
    summary_result = _chat(summary_msg, max_tokens=200)
    summary_result = _strip_code_fences(summary_result)
    summary = json.loads(summary_result)

    # Second pass: extract transactions in batches
    all_transactions = []
    chunk_size = 4000
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        if not chunk.strip():
            continue

        tx_msg = [{
            "role": "user",
            "content": f"""Extract ONLY the financial transactions from this bank statement text. Return a JSON array. Each item: {{"date":"YYYY-MM-DD","description":"short merchant name","amount":0.00,"type":"deposit|withdrawal","category":"Cat"}}
Categories: Groceries, Dining, Fuel, Utilities, Insurance, Rent/Mortgage, Entertainment, Shopping, Transfer, Income, Other
Keep descriptions SHORT. Skip headers, summaries, balances — only actual transactions.
If no transactions in this text, return [].
Return ONLY valid JSON array, nothing else.

{chunk}""",
        }]
        tx_result = _chat(tx_msg, max_tokens=2000)
        tx_result = _strip_code_fences(tx_result)
        try:
            txns = json.loads(tx_result)
            if isinstance(txns, list):
                all_transactions.extend(txns)
        except (json.JSONDecodeError, ValueError):
            continue  # skip unparseable chunks

    # Deduplicate by date+description+amount
    seen = set()
    unique = []
    for tx in all_transactions:
        key = (tx.get("date", ""), tx.get("description", ""), tx.get("amount", 0))
        if key not in seen:
            seen.add(key)
            unique.append(tx)

    summary["transactions"] = unique
    summary["_raw_text"] = text[:2000]  # first 2k chars for account detection
    return summary


def _strip_code_fences(text):
    """Strip markdown code fences from AI output."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()
