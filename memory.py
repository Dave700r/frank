"""Access Frank's semantic memory via the MCP server REST API."""
import json
import httpx

MEMORY_URL = "http://localhost:8765/search"


def search(query, n_results=5):
    """Search Frank's memory. Returns list of relevant memory chunks."""
    try:
        resp = httpx.post(
            MEMORY_URL,
            json={"query": query, "n_results": n_results},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # The response contains the search results as a string
        result = data.get("result", "")
        if isinstance(result, str):
            return result
        return json.dumps(result)
    except Exception as e:
        return f"Memory search unavailable: {e}"
