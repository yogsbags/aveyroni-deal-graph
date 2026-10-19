"""Search public LinkedIn posts through harvestapi/linkedin-post-search.

No cookies and no profile email lookup. One run is about $0.002 per post
plus a fraction of a cent to start. Comments and reactions stay off unless
a scan asks for comments, which is a separate per-comment charge.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

ACTOR = "harvestapi~linkedin-post-search"
COMMENTS_ACTOR = "harvestapi~linkedin-post-comments"
API = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


class ApifyError(RuntimeError):
    pass


def token() -> str:
    return os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN") or ""


def search_posts(
    queries: list[str] | None = None,
    author_urls: list[str] | None = None,
    max_posts: int = 12,
    posted_limit: str = "month",
    scrape_comments: bool = False,
    max_comments: int = 8,
    profile_mode: str = "short",
    timeout: int = 280,
) -> list[dict]:
    key = token()
    if not key:
        raise ApifyError("Set APIFY_TOKEN before scanning LinkedIn. Nothing was charged.")
    if not queries and not author_urls:
        raise ApifyError("A scan needs a search query or profile URLs already in the graph.")
    payload: dict = {
        "maxPosts": max(1, min(int(max_posts), 50)),
        "postedLimit": posted_limit if posted_limit in {"any", "1h", "24h", "week", "month", "3months", "6months", "year"} else "month",
        "sortBy": "relevance",
        "profileScraperMode": profile_mode if profile_mode in {"short", "main"} else "short",
        "scrapeReactions": False,
        "scrapeComments": bool(scrape_comments),
    }
    if queries:
        payload["searchQueries"] = [q.strip() for q in queries if q and q.strip()]
    if author_urls:
        payload["authorUrls"] = author_urls[:12]
    if scrape_comments:
        payload["maxComments"] = max(1, min(int(max_comments), 15))
        payload["commentsProfileScraperMode"] = "short"
    url = API.format(actor=ACTOR) + "?token=" + key
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise ApifyError(f"Apify returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApifyError(f"Could not reach Apify: {exc.reason}") from exc
    data = json.loads(body)
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if not isinstance(data, list):
        raise ApifyError("Apify returned an unexpected payload.")
    return [row for row in data if isinstance(row, dict)]


def fetch_comments(post_urls: list[str], max_items: int = 6, timeout: int = 240) -> list[dict]:
    """Public comments on known post URLs. About $0.002 per comment, no cookies."""
    key = token()
    if not key:
        raise ApifyError("Set APIFY_TOKEN before reading LinkedIn comments. Nothing was charged.")
    urls = [url.strip() for url in post_urls if url and url.strip().startswith("http")]
    if not urls:
        return []
    payload = {
        "posts": urls,
        "maxItems": max(1, min(int(max_items), 15)),
        "postedLimit": "any",
        "scrapeReplies": False,
        "profileScraperMode": "short",
    }
    url = API.format(actor=COMMENTS_ACTOR) + "?token=" + key
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise ApifyError(f"Apify returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApifyError(f"Could not reach Apify: {exc.reason}") from exc
    data = json.loads(body)
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if not isinstance(data, list):
        raise ApifyError("Apify returned an unexpected payload.")
    return [row for row in data if isinstance(row, dict)]
