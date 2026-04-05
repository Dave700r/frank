"""Mem0 memory integration for Frank.
Provides intelligent memory with entity extraction, deduplication, and semantic search.
Uses Ollama for embeddings and OpenRouter for extraction."""
import os
import ssl
import logging
from pathlib import Path

import config as app_config

# Conditionally disable SSL verification for self-signed certs
if app_config.MEM0_SKIP_SSL_VERIFY:
    ssl._create_default_https_context = ssl._create_unverified_context
    os.environ["CURL_CA_BUNDLE"] = ""

    import httpx
    _original_client = httpx.Client
    _original_async_client = httpx.AsyncClient

    class _NoVerifyClient(_original_client):
        def __init__(self, *args, **kwargs):
            kwargs["verify"] = False
            super().__init__(*args, **kwargs)

    class _NoVerifyAsyncClient(_original_async_client):
        def __init__(self, *args, **kwargs):
            kwargs["verify"] = False
            super().__init__(*args, **kwargs)

    httpx.Client = _NoVerifyClient
    httpx.AsyncClient = _NoVerifyAsyncClient

from mem0 import Memory

log = logging.getLogger("family-bot.mem0")

_memory = None
_data_dir = Path(app_config._paths.get("data_dir", Path(__file__).parent / "data"))


def get_memory():
    """Lazy-init Mem0 instance."""
    global _memory
    if _memory is not None:
        return _memory

    mem0_data_path = str(_data_dir / "mem0_data") if not Path(app_config._paths.get("mem0_data", "")).is_absolute() else app_config._paths.get("mem0_data", "")
    mem0_history_path = str(_data_dir / "mem0_history.db")

    config = {
        "llm": {
            "provider": "openai",
            "config": {
                "model": app_config.MEM0_LLM_MODEL,
                "api_key": os.environ.get("OPENROUTER_API_KEY", ""),
                "openai_base_url": "https://openrouter.ai/api/v1",
                "temperature": 0,
                "max_tokens": 1000,
            },
        },
        "embedder": {
            "provider": "ollama",
            "config": {
                "model": app_config.MEM0_OLLAMA_MODEL,
                "ollama_base_url": app_config.MEM0_OLLAMA_BASE_URL,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "frank_mem0",
                "path": mem0_data_path,
                "embedding_model_dims": 768,
            },
        },
        "history_db_path": mem0_history_path,
        "version": "v1.1",
    }

    _memory = Memory.from_config(config)
    log.info("Mem0 initialized")
    return _memory


def add(text, user_id="family", metadata=None):
    """Add a memory from a conversation. Mem0 auto-extracts facts."""
    try:
        m = get_memory()
        result = m.add(text, user_id=user_id, metadata=metadata or {})
        if result and result.get("results"):
            facts = [r.get("memory", "") for r in result["results"] if r.get("event") in ("ADD", "UPDATE")]
            if facts:
                log.info(f"Mem0 extracted {len(facts)} facts for {user_id}")
            return facts
        return []
    except Exception as e:
        log.error(f"Mem0 add error: {e}")
        return []


def search(query, user_id="family", limit=5):
    """Search memories. Returns list of relevant memory strings."""
    try:
        m = get_memory()
        results = m.search(query, user_id=user_id, limit=limit)
        if results and results.get("results"):
            return [r["memory"] for r in results["results"] if r.get("memory")]
        return []
    except Exception as e:
        log.error(f"Mem0 search error: {e}")
        return []


def get_all(user_id="family"):
    """Get all memories for a user."""
    try:
        m = get_memory()
        results = m.get_all(user_id=user_id)
        if results and results.get("results"):
            return [r["memory"] for r in results["results"] if r.get("memory")]
        return []
    except Exception as e:
        log.error(f"Mem0 get_all error: {e}")
        return []


def add_conversation(user_name, user_message, frank_reply):
    """Add a full conversation exchange. Mem0 extracts relevant facts automatically."""
    text = f"{user_name}: {user_message}\nFrank: {frank_reply}"
    return add(text, user_id=user_name.lower())
