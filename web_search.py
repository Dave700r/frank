"""Web search via Tavily API."""
import os
import httpx

TAVILY_URL = "https://api.tavily.com/search"


def search(query, max_results=5):
    """Search the web and return summarized results."""
    try:
        resp = httpx.post(
            TAVILY_URL,
            json={
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_answer": True,
            },
            headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        answer = data.get("answer", "")
        results = []
        for r in data.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", "")[:300],
            })

        return {"answer": answer, "results": results}
    except Exception as e:
        return {"answer": f"Search failed: {e}", "results": []}
