"""Add one normalized LinkedIn post to the growing graph."""

from __future__ import annotations

import hashlib
import re

from classify import classify
from store import GraphStore

SIDES = {"sell": "Direct seller", "buy": "Direct buyer", "intermediary": "Intermediary", "signal": "Other"}


def post_key(post: dict) -> str:
    url = post.get("url") or ""
    activity = re.search(r"activity[:-](\d+)", url)
    if activity:
        return "activity:" + activity.group(1)
    if post.get("id"):
        return "id:" + re.sub(r"\W+", "", str(post["id"]))[:80]
    digest = hashlib.sha1((post.get("content") or "").encode()).hexdigest()[:16]
    return "text:" + digest


def _slug(prefix: str, value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return f"{prefix}-{token or 'unknown'}"[:80]


def person_id(name: str, profile: str) -> str:
    match = re.search(r"linkedin\.com/(in|company)/([^/?#]+)", profile or "", re.I)
    if match:
        return _slug("person", match.group(1) + "-" + match.group(2))
    return _slug("person", name)


def _short(label: str, limit: int = 28) -> str:
    label = re.sub(r"\s+", " ", label).strip()
    if len(label) <= limit:
        return label
    return label[: limit - 1].rstrip() + "…"


def _deal_label(post: dict, side: str) -> str:
    content = re.sub(r"\s+", " ", post.get("content") or "").strip()
    sentence = re.split(r"(?<=[.!?])\s", content)[0]
    sentence = sentence[:90].strip()
    prefix = {"sell": "Sell", "buy": "Buy", "intermediary": "Mandate", "signal": "Signal"}.get(side, "Deal")
    return f"{prefix} · {sentence}" if sentence else f"{prefix} · {post.get('author_name') or 'LinkedIn post'}"


def _apply(store: GraphStore, node: dict, created: list[dict]) -> None:
    fresh = store.upsert_node(node)
    if fresh:
        created.append(fresh)


def _link(store: GraphStore, from_id: str, to_id: str, rel: str, note: str, created: list[dict]) -> None:
    edge = store.add_edge(from_id, to_id, rel, note)
    if edge:
        created.append(edge)


def ingest_post(store: GraphStore, post: dict, sector_hint: str = "", status: str = "live", verdict=None, state: str = "open") -> dict:
    """Insert a post and its people. Returns new nodes and edges, empty when already stored."""
    key = post_key(post)
    if store.seen_post(key):
        if verdict is None or verdict.skip:
            return {"nodes": [], "edges": [], "skipped": True, "reason": "already ingested"}
        store._db.execute("DELETE FROM posts WHERE key=? AND side='skipped'", (key,))
        store._db.commit()
        if store.seen_post(key):
            return {"nodes": [], "edges": [], "skipped": True, "reason": "already ingested"}

    if verdict is None:
        verdict = classify(post.get("content") or "", post.get("author_headline") or "", sector_hint)
    if verdict.skip:
        store.remember_post(key, post.get("url") or "", "skipped", "", "")
        return {"nodes": [], "edges": [], "skipped": True, "reason": verdict.why}

    nodes: list[dict] = []
    edges: list[dict] = []
    sector_id = _slug("sector", verdict.sector)
    _apply(store, {
        "id": sector_id,
        "kind": "sector",
        "label": verdict.sector,
        "short": verdict.sector,
        "status": status,
    }, nodes)
    _link(store, store.root_id(), sector_id, "sector", "", edges)

    deal_id = "deal-" + hashlib.sha1(key.encode()).hexdigest()[:12]
    label = _deal_label(post, verdict.side)
    if verdict.person_side == "intermediary":
        label = re.sub(r"^(Sell|Buy|Mandate|Signal) · ", "Intermediary · ", label)
    if state == "closed":
        label = "Closed · " + re.sub(r"^(Sell|Buy|Mandate|Signal|Intermediary) · ", "", label)
    _apply(store, {
        "id": deal_id,
        "kind": "deal",
        "side": verdict.side,
        "label": label,
        "short": _short(label, 32),
        "sub": post.get("author_headline") or post.get("author_name") or "",
        "posted": post.get("posted") or "",
        "value": post.get("value") or "",
        "url": post.get("url") or "",
        "status": "Closed" if state == "closed" else status,
        "state": state,
        "note": verdict.why,
    }, nodes)
    _link(store, sector_id, deal_id, "sector", verdict.sector, edges)

    author = person_id(post.get("author_name") or "Unknown", post.get("author_profile") or "")
    _apply(store, {
        "id": author,
        "kind": "person",
        "side": verdict.person_side,
        "label": post.get("author_name") or "Unknown",
        "short": _short(post.get("author_name") or "Unknown", 22),
        "sub": post.get("author_headline") or SIDES.get(verdict.person_side, ""),
        "profile": post.get("author_profile") or "",
        "email": post.get("email") or "",
        "phone": post.get("phone") or "",
        "status": status,
        "note": verdict.why,
    }, nodes)
    _link(store, author, deal_id, "posted", "", edges)

    for comment in (post.get("comments") or [])[:8]:
        _attach_comment(store, deal_id, verdict.side, comment, status, nodes, edges)

    if state != "closed" and verdict.side in {"sell", "buy"}:
        for other in store.opposite_deals(sector_id, verdict.side):
            _link(store, deal_id, other, "match", "same sector, opposite side", edges)

    store.remember_post(key, post.get("url") or "", verdict.side, sector_id, deal_id)
    return {"nodes": nodes, "edges": edges, "skipped": False, "side": verdict.side, "deal_id": deal_id}


def attach_comment(store: GraphStore, post_url: str, comment: dict, status: str = "live") -> dict:
    deal_id = store.deal_for_url(post_url)
    if not deal_id:
        return {"nodes": [], "edges": []}
    deal = store.get_node(deal_id) or {}
    nodes: list[dict] = []
    edges: list[dict] = []
    _attach_comment(store, deal_id, deal.get("side") or "signal", comment, status, nodes, edges)
    return {"nodes": nodes, "edges": edges}


def _attach_comment(store, deal_id, deal_side, comment, status, nodes, edges) -> None:
    name = comment.get("name") or "Unknown"
    pid = person_id(name, comment.get("profile") or "")
    comment_verdict = classify(comment.get("text") or "", comment.get("headline") or "")
    if comment_verdict.skip:
        side = "signal"
        why = "engaged with the post"
    else:
        side = comment_verdict.person_side
        why = comment_verdict.why
    _apply(store, {
        "id": pid,
        "kind": "person",
        "side": side,
        "label": name,
        "short": _short(name, 22),
        "sub": comment.get("headline") or "",
        "profile": comment.get("profile") or "",
        "status": status,
        "note": why,
    }, nodes)
    _link(store, pid, deal_id, "commented", _short(comment.get("text") or "", 80), edges)
    if side in {"sell", "buy"} and deal_side in {"sell", "buy"} and side != deal_side:
        _link(store, pid, deal_id, "match", "commenter is the other side", edges)
