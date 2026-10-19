"""Find commenters on deal posts and let Hermes place them.

Public comments come from the post-comments actor. Hermes decides whether
each person is a buyer, a seller, an intermediary, or not a party.
"""

from __future__ import annotations

import csv
import http.client
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from apify_source import fetch_comments
from normalize import normalize_comment_item
from hermes_classify import classify_comments, load_cache
from hospitality_scan import install_token
from ingest import _short, person_id
from store import GraphStore

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "graph.db"
RAW_COMMENTS = ROOT / "data" / "post-comments.jsonl"
CACHE = ROOT / "data" / "comment-classes.json"
URL_MAP = ROOT / "data" / "comment-url-map.json"
BATCH_URLS = 10


def resolve(url: str) -> str:
    if "lnkd.in/" not in url:
        return url.split("?")[0]
    parts = urlparse(url)
    conn = http.client.HTTPSConnection(parts.netloc, timeout=20)
    try:
        conn.request("HEAD", parts.path or "/", headers={"User-Agent": "Mozilla/5.0"})
        resp = conn.getresponse()
        loc = resp.getheader("Location") or url
    except OSError:
        loc = url
    finally:
        conn.close()
    return loc.split("?")[0]


def digits(value: str) -> list[str]:
    found = []
    token = ""
    for char in value or "":
        if char.isdigit():
            token += char
        else:
            if len(token) >= 15:
                found.append(token)
            token = ""
    if len(token) >= 15:
        found.append(token)
    return found


def sheet_comments(field: str, author: str) -> list[dict]:
    text = (field or "").strip()
    if not text:
        return []
    out = []
    for part in text.split("|"):
        part = part.strip()
        if not part or part.lower().startswith("note:"):
            continue
        lowered = part.lower()
        if lowered.startswith("(") and ("not captured" in lowered or "more comment" in lowered or "not individually" in lowered):
            continue
        title = ""
        if "):" in part:
            left, body = part.split("):", 1)
            if "(" not in left:
                continue
            name, title = left.rsplit("(", 1)
        elif ":" in part:
            name, body = part.split(":", 1)
        else:
            continue
        name, title, body = name.strip(" ."), title.strip(), body.strip()
        if "->" in body:
            body = body.split("->", 1)[0].strip()
        body = body.strip("'\"“”").strip()
        if not name or name.casefold() == author.casefold() or "author" in title.casefold():
            continue
        out.append({"name": name, "headline": title, "text": body or "commented", "profile": ""})
    return out


def load_deals(db) -> list[dict]:
    rows = db.execute(
        """SELECT n.id, n.url, n.label, n.side, n.note, p.label AS poster
           FROM nodes n
           LEFT JOIN edges e ON e.to_id=n.id AND e.rel='posted'
           LEFT JOIN nodes p ON p.id=e.from_id
           WHERE n.kind='deal'"""
    ).fetchall()
    return [dict(row) for row in rows]


def index_deals(deals: list[dict]) -> dict[str, str]:
    index = {}
    for deal in deals:
        for token in digits(deal.get("url") or ""):
            index.setdefault(token, deal["id"])
        if deal.get("url"):
            index.setdefault(deal["url"].split("?")[0], deal["id"])
    return index


def comment_from_item(item: dict) -> dict | None:
    actor = item.get("actor") or item.get("author") or {}
    if not isinstance(actor, dict):
        actor = {}
    name = (actor.get("name") or "").strip()
    text = (item.get("commentary") or item.get("text") or "").strip()
    if not name or not text:
        return None
    return {
        "name": name,
        "headline": (actor.get("position") or actor.get("headline") or actor.get("info") or "").strip(),
        "text": text,
        "profile": (actor.get("linkedinUrl") or "").strip(),
        "post_ids": digits(str(item.get("postId") or "")) + digits(json.dumps(item.get("query") or "")),
    }


def fetch_missing(deals: list[dict]) -> None:
    install_token()
    seen_inputs = set()
    if RAW_COMMENTS.exists():
        for line in RAW_COMMENTS.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("source"):
                seen_inputs.add(row["source"])
    need = []
    for deal in deals:
        url = (deal.get("url") or "").split("?")[0]
        if not url.startswith("http"):
            continue
        if url in seen_inputs:
            continue
        # Deals that already have commenters were scraped earlier.
        need.append(deal)
    # Only fetch deals that have no comment edge yet. Existing threads are classified from stored text.
    db = sqlite3.connect(DB)
    have = {row[0] for row in db.execute("SELECT DISTINCT to_id FROM edges WHERE rel='commented'")}
    db.close()
    targets = []
    url_map = {}
    if URL_MAP.exists():
        url_map = json.loads(URL_MAP.read_text())
    for deal in need:
        if deal["id"] in have:
            continue
        original = deal["url"].split("?")[0]
        if original in seen_inputs:
            continue
        resolved = resolve(original)
        targets.append((deal["id"], original, resolved))
        deal["resolved"] = resolved
        for token in digits(resolved):
            url_map.setdefault(token, deal["id"])
    URL_MAP.write_text(json.dumps(url_map))
    print(f"fetching comments for {len(targets)} posts", flush=True)
    RAW_COMMENTS.parent.mkdir(parents=True, exist_ok=True)
    for offset in range(0, len(targets), BATCH_URLS):
        chunk = targets[offset : offset + BATCH_URLS]
        urls = [resolved for _deal, _original, resolved in chunk]
        originals = [original for _deal, original, _resolved in chunk]
        print(f"comments {offset + 1}-{offset + len(chunk)} of {len(targets)}", flush=True)
        try:
            items = fetch_comments(urls, max_items=6, timeout=220)
        except Exception as err:
            print(f"batch failed ({err}); one by one", flush=True)
            items = []
            for url in urls:
                try:
                    items.extend(fetch_comments([url], max_items=6, timeout=120))
                except Exception as one:
                    print(f"skip {url[:60]} {one}", flush=True)
        with RAW_COMMENTS.open("a", encoding="utf-8") as handle:
            for url in originals:
                handle.write(json.dumps({"source": url}, ensure_ascii=False) + "\n")
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"  {len(items)} comments", flush=True)


def gather(deals: list[dict]) -> list[dict]:
    by_id = {deal["id"]: deal for deal in deals}
    index = index_deals(deals)
    if URL_MAP.exists():
        for token, deal_id in json.loads(URL_MAP.read_text()).items():
            index.setdefault(token, deal_id)
    for deal in deals:
        if deal.get("resolved"):
            for token in digits(deal["resolved"]):
                index.setdefault(token, deal["id"])
    comments = []
    seen = set()

    def add(deal_id: str, name: str, headline: str, text: str, profile: str) -> None:
        deal = by_id.get(deal_id)
        if not deal or not name or name.casefold() == "unknown":
            return
        if deal.get("poster") and name.casefold() == deal["poster"].casefold():
            return
        key = (deal_id, profile or name.casefold())
        if key in seen:
            return
        seen.add(key)
        comments.append({
            "id": f"{deal_id}:{person_id(name, profile)}",
            "deal_id": deal_id,
            "deal_side": deal.get("side") or "",
            "deal": f"{deal.get('label') or ''}. {deal.get('note') or ''}",
            "name": name,
            "headline": headline,
            "text": text,
            "profile": profile,
        })

    hospitality = ROOT / "data" / "hospitality-raw.jsonl"
    if hospitality.exists():
        for line in hospitality.read_text().splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed = normalize_comment_item(item)
            if not parsed or not parsed.get("name"):
                continue
            deal_id = index.get(parsed.get("post_activity") or "")
            if deal_id:
                add(deal_id, parsed["name"], parsed.get("headline") or "", parsed.get("text") or "", parsed.get("profile") or "")

    if RAW_COMMENTS.exists():
        for line in RAW_COMMENTS.read_text().splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("source") and not item.get("commentary") and not item.get("actor"):
                continue
            parsed = comment_from_item(item)
            if not parsed:
                continue
            deal_id = ""
            for token in parsed["post_ids"]:
                if token in index:
                    deal_id = index[token]
                    break
            if deal_id:
                add(deal_id, parsed["name"], parsed["headline"], parsed["text"], parsed["profile"])

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    for row in db.execute(
        """SELECT e.to_id deal_id, p.label name, p.sub headline, p.profile, e.note text
           FROM edges e JOIN nodes p ON p.id=e.from_id
           WHERE e.rel='commented'"""
    ):
        add(row["deal_id"], row["name"], row["headline"] or "", row["text"] or "", row["profile"] or "")

    sheet = ROOT / "data" / "sheets" / "mandates.csv"
    if sheet.exists():
        for row in csv.DictReader(sheet.open(encoding="utf-8")):
            num = (row.get("#") or "").strip()
            url = (row.get("Post URL") or "").strip()
            deal_id = index.get(url.split("?")[0], "")
            if not deal_id and num:
                needle = "".join(ch for ch in f"sheet-{num}" if ch.isalnum())
                for key in (f"id:{needle}", f"id:{needle}buy", f"id:{needle}sell"):
                    found = db.execute("SELECT deal_id FROM posts WHERE key=?", (key,)).fetchone()
                    if found:
                        deal_id = found["deal_id"]
                        break
            if not deal_id:
                continue
            for comment in sheet_comments(row.get("Commenters (Name - Title: Comment)") or "", row.get("Post Author") or ""):
                add(deal_id, comment["name"], comment["headline"], comment["text"], "")
    db.close()
    return comments


def apply(store: GraphStore, comments: list[dict], verdicts: dict[str, dict]) -> dict:
    db = store._db
    posters = {row[0] for row in db.execute("SELECT DISTINCT from_id FROM edges WHERE rel='posted'")}
    kept = skipped = removed = 0
    for comment in comments:
        verdict = verdicts.get(comment["id"]) or {}
        relevant = bool(verdict.get("relevant")) and verdict.get("person_side") in {"sell", "buy", "intermediary"}
        pid = person_id(comment["name"], comment.get("profile") or "")
        edge_id = f"commented:{pid}:{comment['deal_id']}"
        if not relevant:
            if db.execute("SELECT 1 FROM edges WHERE id=?", (edge_id,)).fetchone():
                db.execute("DELETE FROM edges WHERE id=?", (edge_id,))
                db.execute(
                    "DELETE FROM edges WHERE rel='match' AND from_id=? AND to_id=?",
                    (pid, comment["deal_id"]),
                )
                removed += 1
            continue
        side = verdict["person_side"]
        current = db.execute("SELECT id, side, sub FROM nodes WHERE id=?", (pid,)).fetchone()
        if current is None:
            store.upsert_node({
                "id": pid,
                "kind": "person",
                "side": side,
                "label": comment["name"],
                "short": _short(comment["name"], 22),
                "sub": comment.get("headline") or "",
                "profile": comment.get("profile") or "",
                "status": "live",
                "note": verdict.get("why") or "",
            })
        elif pid not in posters:
            db.execute(
                "UPDATE nodes SET side=?, sub=COALESCE(NULLIF(sub,''), ?), profile=COALESCE(NULLIF(profile,''), ?), note=? WHERE id=?",
                (side, comment.get("headline") or "", comment.get("profile") or "", verdict.get("why") or "", pid),
            )
        elif comment.get("headline") and current["sub"] in (None, ""):
            db.execute("UPDATE nodes SET sub=? WHERE id=?", (comment["headline"], pid))
        store.add_edge(pid, comment["deal_id"], "commented", _short(comment.get("text") or "", 80))
        deal_side = comment.get("deal_side")
        if side in {"sell", "buy"} and deal_side in {"sell", "buy"} and side != deal_side:
            store.add_edge(pid, comment["deal_id"], "match", "commenter is the other side")
        kept += 1
    # Drop comment-only people who no longer connect to anything.
    orphans = db.execute(
        """SELECT id FROM nodes WHERE kind='person' AND status='live'
           AND id NOT IN (SELECT from_id FROM edges UNION SELECT to_id FROM edges)"""
    ).fetchall()
    for orphan in orphans:
        db.execute("DELETE FROM nodes WHERE id=?", (orphan["id"],))
    db.commit()
    return {"kept": kept, "removed": removed, "skipped": skipped, "orphans": len(orphans)}


def main() -> None:
    store = GraphStore(DB)
    deals = load_deals(store._db)
    fetch_missing(deals)
    comments = gather(deals)
    print(f"commenters to classify {len(comments)}", flush=True)
    verdicts = classify_comments(comments, load_cache(CACHE), CACHE)
    summary = apply(store, comments, verdicts)
    print({**summary, "graph": store.counts()}, flush=True)
    store.close()


if __name__ == "__main__":
    main()
