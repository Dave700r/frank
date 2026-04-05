# Frank

A self-hosted AI family assistant that lives in your Matrix chat. Frank manages groceries, tracks finances, handles email, sets reminders, learns your family's habits, and delivers morning briefings — all running on hardware you own.

## What Frank Does

- **Grocery & Inventory** — shopping lists, stock tracking, low-stock alerts, consumption-based reorder suggestions
- **Finance** — expense logging, monthly summaries, account balances, bank/credit card statement parsing (PDF), receipt scanning (images) — all via [Firefly III](https://www.firefly-iii.org/)
- **Email** — reads your inbox (IMAP or Gmail API), scans for bills, sends emails on your behalf (via AgentMail)
- **Photos** — search and share family photos from [Immich](https://immich.app/) by text, date, person, or album
- **Reminders** — natural language reminders ("remind me in 30 minutes to check the oven"), follow-up scheduling
- **Morning Briefings** — weather, commute times, grocery status, crypto prices, delivered to the family group every morning
- **Recipes** — searchable family recipe database
- **Memory** — remembers conversations, learns facts about your family, consolidates memories overnight
- **Personality** — dry humor, direct communication, adapts tone by time of day, learns each family member's communication style
- **Companion Pets** — Tamagotchi-style buddies for each family member (because why not)

## Stack

- **Python 3.12+** with async throughout
- **Matrix** (end-to-end encrypted) via [matrix-nio](https://github.com/matrix-nio/matrix-nio)
- **Telegram** (optional) via [python-telegram-bot](https://python-telegram-bot.org/) — can run alongside or instead of Matrix
- **AI** via [OpenRouter](https://openrouter.ai/) (Claude Haiku by default, Gemini Flash for vision)
- **Firefly III** for finance tracking (optional)
- **Mem0** + Ollama for semantic memory (optional)
- Runs on a **Raspberry Pi 5**, any Linux server, or Docker

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/Dave700r/frank.git
cd frank
cp config.yaml.example config.yaml
```

Edit `config.yaml` with your Matrix homeserver, bot account, and family members. See the comments in the file for guidance.

### 2. Set environment variables

Create a `.env` file or export these:

```bash
MATRIX_BOT_PASSWORD=your_bot_password
OPENROUTER_API_KEY=your_openrouter_key

# Optional:
FIREFLY_TOKEN=your_firefly_token
EMAIL_USER=your_email
EMAIL_PASS=your_email_password
AGENTMAIL_API_KEY=your_agentmail_key
TAVILY_API_KEY=your_tavily_key
TOMTOM_API_KEY=your_tomtom_key
```

### 3a. Run with Docker (recommended)

```bash
docker compose up -d
```

### 3b. Run directly

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python matrix_bot.py
```

### 4. Talk to Frank

Invite your bot user to a Matrix room and start chatting. Frank responds to natural language and also supports commands:

```
!list          Shopping list
!add <item>    Add to list
!bought <item> Mark as bought
!stock         Full inventory
!spent <$> <store>  Log a purchase
!summary       Monthly spending
!balance       Account balances (DM)
!inbox         Check emails (DM)
!bills         Recent bills (DM)
!remind <when> <what>  Set a reminder
!recipes       Browse recipes
!buddy         Your companion pet
!briefing      Morning briefing on demand
!help          All commands
```

## Configuration

All configuration lives in `config.yaml`. Every feature is optional — disable what you don't need:

| Feature | Config flag | Required env vars | External service |
|---------|------------|-------------------|-----------------|
| Matrix chat | *(always on)* | `MATRIX_BOT_PASSWORD` | Matrix homeserver |
| Telegram | `telegram.enabled` | `TELEGRAM_BOT_TOKEN` | Telegram BotFather |
| AI responses | *(always on)* | `OPENROUTER_API_KEY` | OpenRouter |
| Finance | `firefly.enabled` | `FIREFLY_TOKEN` | Firefly III |
| Owner email (IMAP) | `email.enabled` | `EMAIL_USER`, `EMAIL_PASS` | IMAP server |
| Owner email (Gmail) | `gmail.enabled` | *(OAuth2 flow)* | Gmail API |
| Bot email | `agentmail.enabled` | `AGENTMAIL_API_KEY` | AgentMail |
| Photos | `immich.enabled` | *(API key in config)* | Immich |
| Memory | `mem0.enabled` | — | Ollama |
| Web search | `web_search.enabled` | `TAVILY_API_KEY` | Tavily |
| Voice API | `voice.enabled` | — | — |
| Commute times | `briefing.commutes` | `TOMTOM_API_KEY` | TomTom |

## Architecture

```
matrix_bot.py          Entry point, scheduler
matrix_client.py       Matrix E2E client, command routing, message handling
telegram_client.py     Telegram bot client (optional)
ai.py                  AI layer (OpenRouter), context building, vision
config.py              YAML config loader
frank_persona.py       Personality, generated from config
prompt_builder.py      Modular prompt assembly

db.py                  Inventory/shopping/finance (SQLite)
firefly.py             Firefly III API
email_client.py        IMAP/SMTP
gmail_client.py        Gmail API (OAuth2)
agentmail_client.py    AgentMail API
immich_client.py       Immich photo library API
briefing.py            Morning briefing data collection
reminders.py           Reminder system (SQLite)
recipes.py             Recipe database (SQLite)
conversation_log.py    Daily conversation logging
memory.py              Chroma vector search (MCP)
mem0_memory.py         Mem0 structured memory
episodes.py            Episodic memory + follow-ups
dream.py               Overnight memory consolidation
humanize.py            Typing delays, message batching, engagement scoring
style_learner.py       Per-user communication style learning
buddy.py               Tamagotchi companion pets
coordinator.py         Multi-agent parallel task execution
ultraplan.py           Extended thinking for complex requests
permissions.py         Action risk classification
voice_api.py           HTTP API for voice integration
web_search.py          Tavily web search
```

## Matrix Setup

Frank needs a Matrix account on your homeserver. Create a bot user (e.g., `@frank:your.server`), then:

1. Set `MATRIX_BOT_PASSWORD` in your environment
2. Configure the homeserver URL and bot user in `config.yaml`
3. Invite the bot to your family room
4. Frank auto-joins and starts responding

E2E encryption is handled automatically. Frank trusts all devices in rooms he's invited to.

## Personalizing Frank

Frank's personality is built from your `config.yaml`:

- **Family members** — names, nicknames, Matrix IDs
- **Owner** — who can access email/finance (admin commands)
- **Spanish learners** — members who get Spanish mixed into conversations
- **Persona file** — point `persona_file` at a custom YAML for full personality override

The default personality is opinionated, direct, and has dry humor. Override it if that's not your family's vibe.

## License

Apache-2.0
