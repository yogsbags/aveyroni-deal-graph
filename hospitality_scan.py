"""Pull hospitality buy-side and sell-side LinkedIn posts into the deal graph.

Pass 1 uses a fixed keyword list. Pass 2 searches phrases that actually
showed up in those posts. Commenters are stored with their headlines.
Signals and posts outside hospitality are left out of the graph.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from apify_source import search_posts
from classify import Classification
from hermes_classify import INDIA_PROMPT, classify_closed, classify_mandates, classify_posts, load_cache, save_cache
from ingest import ingest_post
from normalize import activity_id, normalize_comment_item, normalize_post
from store import GraphStore

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("DEAL_GRAPH_DB") or (ROOT / "data" / "graph.db"))
RAW = ROOT / "data" / "hospitality-raw.jsonl"
CLASSES = ROOT / "data" / "hospitality-classes.json"
TOKEN_FILE = Path("/Users/yogs87/Documents/New project/sebi-aif-monitor/.env")

DIRECT_SELLERS = [
    '"selling our hotel"',
    '"our hotel is for sale"',
    '"our resort is for sale"',
    '"we are selling" hotel',
    '"family owned" "hotel for sale"',
    '"my hotel" "for sale"',
]

INDIA = [
    '"selling our" manufacturing India',
    '"our factory" "for sale" India',
    '"family owned" manufacturing "for sale" India',
    '"NBFC" "for sale" India',
    '"we are selling" "manufacturing unit" India',
    '"industrial business" "for sale" India',
]

MONACO = [
    '"Monaco" "hotel for sale"',
    '"Monaco" "hôtel à vendre"',
    '"Monte Carlo" "for sale"',
    '"Monaco" "looking to acquire"',
    '"Monaco" "off-market"',
    '"Monaco" "acquisition mandate"',
    '"Monaco" "villa for sale"',
    '"Monaco" "apartment for sale"',
    '"Monte Carlo" "hotel for sale"',
    '"Monaco" "seeking a buyer"',
    '"Monaco" "private sale"',
    '"Monaco" immobilier',
    '"Monaco" "hotel portfolio"',
    '"Monaco" "resort"',
    '"Monte Carlo" resort',
    '"Monaco" "boutique hotel"',
    '"Monaco" "fonds de commerce"',
    '"Monaco" "hotel" "keys"',
]

PRELIMINARY = [
    ('"hotel for sale"', "sell"),
    ('"resort for sale"', "sell"),
    ('"hotel" "sell-side mandate"', "sell"),
    ('"hotel portfolio" sale', "sell"),
    ('"looking to acquire" hotel', "buy"),
    ('"acquisition mandate" hospitality', "buy"),
    ('"buy-side" hotel', "buy"),
    ('"seeking a buyer" hotel', "sell"),
]

STOP = {
    "the", "and", "for", "with", "our", "are", "this", "that", "from", "have",
    "has", "was", "were", "will", "your", "you", "not", "but", "its", "into",
    "about", "please", "contact", "looking", "seeking", "hotel", "hotels",
    "resort", "resorts", "hospitality", "sale", "sell", "side", "buy", "acquire",
    "acquisition", "mandate", "https", "www", "com", "linkedin",
}


def install_token() -> None:
    if os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN"):
        return
    if not TOKEN_FILE.exists():
        raise SystemExit("APIFY_TOKEN is not set.")
    for line in TOKEN_FILE.read_text().splitlines():
        if line.startswith("APIFY_TOKEN="):
            os.environ["APIFY_TOKEN"] = line.split("=", 1)[1].strip().strip('"').strip("'")
            return
    raise SystemExit("APIFY_TOKEN is not set.")


CONCRETE = re.compile(
    r"for sale|seeking (a )?buyer|looking to acquire|acquisition mandate|buy-side mandate|"
    r"sell-side mandate.{0,48}(client|owner|ownership|representing)|representing (a |our )?(client|owner)|"
    r"off[- ]market|assets available|outright purchase|bank[- ]owned|\d+\s*keys|"
    r"(?:USD|EUR|GBP|INR|Rs\.?|₹|€|\$)\s?[\d,.]+|\d+\s?(?:crore|million)",
    re.I,
)


ESSAY = re.compile(
    r"a question for anyone|why is institutional|earliest honest sign|statistics do not show|"
    r"second-year students|same brand, similar room counts|noticed an increase in sell-side|"
    r"impresses nobody on the buy side",
    re.I,
)


def keep(post: dict) -> bool:
    verdicts = load_cache(CLASSES)
    verdict = verdicts.get(str(post.get("id") or ""))
    if not verdict:
        return False
    return bool(verdict.get("keep")) and verdict.get("side") in {"sell", "buy"}


def ingest_batch(store: GraphStore, items: list[dict]) -> dict:
    summary = {"posts": 0, "kept": 0, "sell": 0, "buy": 0, "commenters": 0, "commenters_with_headline": 0, "new_deals": 0}
    comments_by_url: dict[str, list[dict]] = {}
    comments_by_activity: dict[str, list[dict]] = {}
    posts = []
    for item in items:
        comment = normalize_comment_item(item)
        if comment:
            if comment.get("post_url"):
                comments_by_url.setdefault(comment["post_url"], []).append(comment)
            if comment.get("post_activity"):
                comments_by_activity.setdefault(comment["post_activity"], []).append(comment)
            continue
        post = normalize_post(item)
        if post:
            posts.append(post)
    for post in posts:
        if not post:
            continue
        summary["posts"] += 1
        if not keep(post):
            continue
        summary["kept"] += 1
        extra = list(comments_by_url.get(post["url"]) or [])
        extra += comments_by_activity.get(activity_id(post.get("url") or "", post.get("id") or "")) or []
        seen = {c.get("profile") or c.get("name") for c in post["comments"]}
        for comment in extra:
            key = comment.get("profile") or comment.get("name")
            if key not in seen:
                post["comments"].append(comment)
                seen.add(key)
        verdicts = load_cache(CLASSES)
        row = verdicts.get(str(post.get("id") or ""))
        verdict = None
        if row:
            side = row.get("side") if row.get("side") in {"sell", "buy", "intermediary", "signal"} else "signal"
            person = row.get("person_side") if row.get("person_side") in {"sell", "buy", "intermediary", "signal"} else side
            verdict = Classification(side, person, 0, 0, int(person == "intermediary"), not row.get("keep"), "Hospitality", row.get("why") or "")
        state = "closed" if row and row.get("state") == "closed" else "open"
        result = ingest_post(store, post, "Hospitality", "live", verdict, state)
        if result.get("skipped"):
            continue
        summary["new_deals"] += 1
        summary[result["side"]] += 1
        summary["commenters"] += len(post["comments"])
        summary["commenters_with_headline"] += sum(1 for comment in post["comments"] if comment.get("headline"))
    return summary


SEED_PHRASES = [
    "operating hotel",
    "off-market hotel",
    "bank owned hotel",
    "outright purchase hotel",
    "hotel assets available",
    "private island resort for sale",
    "operational hotel",
    "income-generating hospitality",
    "leasehold hotel",
    "keys operational hotel",
]


def seed_queries(posts: list[dict], limit: int = 6) -> list[str]:
    already = {query.strip('"').lower() for query, _side in PRELIMINARY}
    counts: Counter[str] = Counter()
    for post in posts:
        text = (post.get("content") or "").lower()
        for phrase in SEED_PHRASES:
            if phrase in text:
                counts[phrase] += 1
    ranked = [phrase for phrase, _n in counts.most_common() if phrase not in already]
    queries = []
    for phrase in ranked:
        if "for sale" in phrase or "available" in phrase or "purchase" in phrase:
            queries.append(f'"{phrase}"')
        else:
            queries.append(f'"{phrase}"')
        if len(queries) >= limit:
            break
    return queries


def append_raw(items: list[dict]) -> None:
    RAW.parent.mkdir(parents=True, exist_ok=True)
    with RAW.open("a", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def ran_searches(path: Path) -> set[str]:
    done = set()
    if not path.exists():
        return done
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        query = json.loads(line).get("query")
        if isinstance(query, dict) and query.get("search"):
            done.add(query["search"])
    return done


def search_queries(queries: list[str], max_posts: int) -> list[dict]:
    found = []
    for query in queries:
        print(f"search {query}", flush=True)
        items = search_posts(
            queries=[query],
            max_posts=max_posts,
            posted_limit="year",
            scrape_comments=True,
            max_comments=8,
            profile_mode="short",
            timeout=240,
        )
        print(f"  {len(items)} items", flush=True)
        if items:
            found.extend(items)
        else:
            found.append({"type": "empty-search", "query": {"search": query, "page": 1, "sortBy": "relevance"}})
    return found


def load_raw_posts() -> list[dict]:
    posts = []
    seen = set()
    if not RAW.exists():
        return posts
    for line in RAW.read_text().splitlines():
        post = normalize_post(json.loads(line))
        if not post or not post.get("id") or post["id"] in seen:
            continue
        seen.add(post["id"])
        posts.append(post)
    return posts


def classify_with_hermes(store: GraphStore) -> dict:
    posts = load_raw_posts()
    verdicts = classify_posts(posts)
    save_cache(CLASSES, verdicts)
    removed = 0
    kept = {"sell": 0, "buy": 0}
    by_id = {post["id"]: post for post in posts}
    rows = store._db.execute("SELECT id, url, label FROM nodes WHERE kind='deal' AND status='live'").fetchall()
    url_to_id = {post.get("url"): post["id"] for post in posts if post.get("url")}
    for row in rows:
        post_id = url_to_id.get(row["url"])
        verdict = verdicts.get(str(post_id or ""))
        deal_id = row["id"]
        if not verdict or not verdict.get("keep") or verdict.get("side") not in {"sell", "buy"}:
            store._db.execute("DELETE FROM edges WHERE from_id=? OR to_id=?", (deal_id, deal_id))
            store._db.execute("DELETE FROM nodes WHERE id=?", (deal_id,))
            store._db.execute("DELETE FROM posts WHERE deal_id=?", (deal_id,))
            removed += 1
            continue
        side = verdict["side"]
        label = re.sub(r"^(Sell|Buy|Mandate|Signal|Intermediary) · ", "", row["label"] or "")
        prefix = "Intermediary" if verdict.get("person_side") == "intermediary" else ("Sell" if side == "sell" else "Buy")
        full = f"{prefix} · {label}"
        short = full if len(full) <= 32 else full[:31].rstrip() + "…"
        store._db.execute(
            "UPDATE nodes SET side=?, label=?, short=?, note=? WHERE id=?",
            (side, full, short, verdict.get("why") or "", deal_id),
        )
        author = store._db.execute(
            "SELECT from_id FROM edges WHERE to_id=? AND rel='posted' LIMIT 1",
            (deal_id,),
        ).fetchone()
        if author:
            store._db.execute(
                "UPDATE nodes SET side=?, note=? WHERE id=? AND status='live'",
                (verdict.get("person_side") or side, verdict.get("why") or "", author["from_id"]),
            )
        kept[side] += 1
        by_id.pop(post_id, None)
    orphans = store._db.execute(
        """SELECT id FROM nodes WHERE kind='person' AND status='live'
           AND id NOT IN (SELECT from_id FROM edges UNION SELECT to_id FROM edges)"""
    ).fetchall()
    for orphan in orphans:
        store._db.execute("DELETE FROM nodes WHERE id=?", (orphan["id"],))
    store._db.commit()
    return {"classified": len(verdicts), "removed": removed, "kept": kept, "graph": store.counts()}


def prune_soft(store: GraphStore) -> int:
    """Drop live deals from this scrape that are commentary rather than a mandate or listing."""
    removed = 0
    rows = store._db.execute("SELECT id, url FROM nodes WHERE kind='deal' AND status='live'").fetchall()
    raw_by_url = {}
    if RAW.exists():
        for line in RAW.read_text().splitlines():
            item = json.loads(line)
            post = normalize_post(item)
            if post and post.get("url"):
                raw_by_url[post["url"]] = post
    for row in rows:
        post = raw_by_url.get(row["url"] or "")
        if post and keep(post):
            continue
        if post is None and row["url"]:
            continue
        deal_id = row["id"]
        store._db.execute("DELETE FROM edges WHERE from_id=? OR to_id=?", (deal_id, deal_id))
        store._db.execute("DELETE FROM nodes WHERE id=?", (deal_id,))
        store._db.execute("DELETE FROM posts WHERE deal_id=?", (deal_id,))
        removed += 1
    orphans = store._db.execute(
        """SELECT id FROM nodes WHERE kind='person' AND status='live'
           AND id NOT IN (SELECT from_id FROM edges UNION SELECT to_id FROM edges)"""
    ).fetchall()
    for orphan in orphans:
        store._db.execute("DELETE FROM nodes WHERE id=?", (orphan["id"],))
    store._db.commit()
    return removed


def main() -> None:
    install_token()
    mode = sys.argv[1] if len(sys.argv) > 1 else "preliminary"
    if mode == "closed":
        store = GraphStore(DB)
        verdicts = load_cache(CLASSES)
        posts = {post["id"]: post for post in load_raw_posts()}
        pending = []
        for pid, row in verdicts.items():
            if row.get("keep"):
                continue
            why = (row.get("why") or "").lower()
            if any(word in why for word in ("hiring", "recruit", "job ", "employment", "applicants", "director of sales", "sales manager")):
                continue
            if pid in posts:
                pending.append(posts[pid])
        print(f"reviewing {len(pending)} posts for closed announcements", flush=True)
        found = []
        for offset in range(0, len(pending), 12):
            chunk = pending[offset:offset + 12]
            print(f"hermes {offset + 1}-{offset + len(chunk)}", flush=True)
            found.extend(classify_closed(chunk))
        closed_ids = set()
        for row in found:
            if row.get("state") != "closed" or row.get("side") not in {"sell", "buy"}:
                continue
            pid = str(row.get("id") or "")
            verdicts[pid] = {
                "id": pid,
                "keep": True,
                "state": "closed",
                "side": row["side"],
                "person_side": row.get("person_side") or "intermediary",
                "why": row.get("why") or "Closed deal announcement",
            }
            closed_ids.add(pid)
        save_cache(CLASSES, verdicts)
        activities = {activity_id(posts[pid].get("url") or "", pid) for pid in closed_ids if pid in posts}
        items = []
        for line in RAW.read_text().splitlines():
            item = json.loads(line)
            post = normalize_post(item)
            if post and post.get("id") in closed_ids:
                items.append(item)
                continue
            comment = normalize_comment_item(item)
            if comment and comment.get("post_activity") in activities:
                items.append(item)
        summary = ingest_batch(store, items)
        print(json.dumps({"closed": len(closed_ids), **summary, "graph": store.counts()}, indent=2), flush=True)
        for row in found:
            if row.get("state") == "closed":
                print(f"closed {row.get('side')} {row.get('why')}", flush=True)
        store.close()
        return
    if mode == "fill":
        store = GraphStore(DB)
        verdicts = load_cache(CLASSES)
        wanted = {pid for pid, row in verdicts.items() if row.get("keep") and row.get("side") in {"sell", "buy"}}
        have = {row["url"] for row in store._db.execute("SELECT url FROM nodes WHERE kind='deal' AND url IS NOT NULL")}
        posts = {post["id"]: post for post in load_raw_posts()}
        missing_ids = {pid for pid in wanted if posts.get(pid) and posts[pid].get("url") not in have}
        activities = {activity_id(posts[pid].get("url") or "", pid) for pid in missing_ids}
        items = []
        for line in RAW.read_text().splitlines():
            item = json.loads(line)
            post = normalize_post(item)
            if post and post.get("id") in missing_ids:
                items.append(item)
                continue
            comment = normalize_comment_item(item)
            if comment and comment.get("post_activity") in activities:
                items.append(item)
        summary = ingest_batch(store, items)
        print(json.dumps({"missing": len(missing_ids), **summary, "graph": store.counts()}, indent=2), flush=True)
        store.close()
        return
    if mode == "hermes":
        store = GraphStore(DB)
        print(json.dumps(classify_with_hermes(store), indent=2), flush=True)
        store.close()
        return
    if mode == "prune":
        store = GraphStore(DB)
        removed = prune_soft(store)
        print(json.dumps({"removed": removed, "graph": store.counts()}, indent=2), flush=True)
        store.close()
        return
    if mode == "india":
        from comment_import import apply as apply_comments
        from ingest import _short

        store = GraphStore(DB)
        raw_path = ROOT / "data" / "india-raw.jsonl"
        cache_path = ROOT / "data" / "india-classes.json"
        max_posts = int(sys.argv[2]) if len(sys.argv) > 2 else 8
        already = ran_searches(raw_path)
        pending = [query for query in INDIA if query not in already]
        print(f"india queries {len(pending)} new, {len(already)} already run", flush=True)
        items = search_queries(pending, max_posts)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        with raw_path.open("a", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        posts, seen, comments_by_post = [], set(), {}
        for item in items:
            comment = normalize_comment_item(item)
            if comment and comment.get("post_activity"):
                comments_by_post.setdefault(comment["post_activity"], []).append(comment)
                continue
            post = normalize_post(item)
            if not post or not post.get("id") or post["id"] in seen:
                continue
            seen.add(post["id"])
            posts.append(post)
        print(f"classifying {len(posts)} India posts", flush=True)
        verdicts = classify_mandates(posts, load_cache(cache_path), cache_path, INDIA_PROMPT)
        kept_comments, seen_comments = [], set()
        kept = direct = 0
        allowed = {"manufacturing", "industrials", "financial services", "nbfc"}
        for post in posts:
            row = verdicts.get(str(post["id"])) or {}
            person = row.get("person_side") if row.get("person_side") in {"sell", "buy", "intermediary", "signal"} else "signal"
            sector = (row.get("sector") or "").strip()
            if not row.get("keep") or row.get("side") != "sell" or row.get("state") == "drop":
                continue
            if person not in {"sell", "intermediary"} or sector.casefold() not in allowed:
                print(f"left off {person:12} {sector:16} {post.get('author_name')} — {row.get('why')}", flush=True)
                continue
            state = "closed" if row.get("state") == "closed" else "open"
            title = " ".join((row.get("label") or "").split()) or " ".join((post.get("content") or "").split())[:90]
            ticket = " ".join((row.get("ticket") or "").split())
            why = " ".join((row.get("why") or "").split())
            activity = activity_id(post.get("url") or "", post.get("id") or "")
            thread = list(post.get("comments") or []) + list(comments_by_post.get(activity) or [])
            payload = dict(post)
            payload["comments"] = []
            payload["value"] = ticket
            payload["email"] = (row.get("email") or post.get("email") or "").strip()
            payload["phone"] = (row.get("phone") or post.get("phone") or "").strip()
            prefix = "Closed" if state == "closed" else ("Intermediary" if person == "intermediary" else "Sell")
            label = f"{prefix} · {title}"
            result = ingest_post(
                store, payload, sector, "live",
                Classification("sell", person, 0, 0, int(person == "intermediary"), False, sector, why),
                state,
            )
            deal_id = result.get("deal_id")
            if not deal_id:
                continue
            kept += 1
            direct += int(person == "sell")
            store._db.execute(
                "UPDATE nodes SET label=?, short=?, value=?, note=? WHERE id=?",
                (label, _short(label, 32), ticket, why, deal_id),
            )
            for comment in thread:
                if not comment.get("name") or comment["name"].casefold() in {"unknown", (post.get("author_name") or "").casefold()}:
                    continue
                marker = (deal_id, comment.get("profile") or comment["name"].casefold())
                if marker in seen_comments:
                    continue
                seen_comments.add(marker)
                kept_comments.append({
                    "id": f"{deal_id}:{comment.get('profile') or comment['name']}",
                    "deal_id": deal_id,
                    "deal_side": "sell",
                    "deal": f"{label}. {why}",
                    "name": comment["name"],
                    "headline": comment.get("headline") or "",
                    "text": comment.get("text") or "",
                    "profile": comment.get("profile") or "",
                })
            print(f"{person:12} sell {sector:20} {ticket[:18]:18} {post.get('author_name')} — {why}", flush=True)
        from mandate_import import merge_same_labels
        merge_same_labels(store, "sector")
        merge_same_labels(store, "person")
        comment_summary = {"kept": 0}
        if kept_comments:
            from hermes_classify import classify_comments
            comment_cache = ROOT / "data" / "india-comment-classes.json"
            comment_verdicts = classify_comments(kept_comments, load_cache(comment_cache), comment_cache)
            comment_summary = apply_comments(store, kept_comments, comment_verdicts)
        store._db.commit()
        print(json.dumps({"posts": len(posts), "kept": kept, "direct_sellers": direct, "comments": comment_summary, "graph": store.counts()}, indent=2), flush=True)
        store.close()
        return
    if mode == "monaco":
        from comment_import import apply as apply_comments
        from ingest import _short

        store = GraphStore(DB)
        raw_path = ROOT / "data" / "monaco-raw.jsonl"
        cache_path = ROOT / "data" / "monaco-classes.json"
        max_posts = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        already = ran_searches(raw_path)
        pending = [query for query in MONACO if query not in already]
        print(f"monaco queries {len(pending)} new, {len(already)} already run", flush=True)
        items = search_queries(pending, max_posts)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        with raw_path.open("a", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        posts = []
        seen = set()
        comments_by_post = {}
        for item in items:
            comment = normalize_comment_item(item)
            if comment and comment.get("post_activity"):
                comments_by_post.setdefault(comment["post_activity"], []).append(comment)
                continue
            post = normalize_post(item)
            if not post or not post.get("id") or post["id"] in seen:
                continue
            seen.add(post["id"])
            posts.append(post)
        print(f"classifying {len(posts)} Monaco posts", flush=True)
        verdicts = classify_mandates(posts, load_cache(cache_path), cache_path)
        kept_comments = []
        seen_comments = set()
        kept = 0
        for post in posts:
            row = verdicts.get(str(post["id"])) or {}
            if not row.get("keep") or row.get("side") not in {"sell", "buy", "both"} or row.get("state") == "drop":
                continue
            sides = ["buy", "sell"] if row.get("side") == "both" else [row.get("side")]
            person = row.get("person_side") if row.get("person_side") in {"sell", "buy", "intermediary", "signal"} else "intermediary"
            state = "closed" if row.get("state") == "closed" else "open"
            sector = (row.get("sector") or "Real estate").strip()
            title = " ".join((row.get("label") or "").split()) or " ".join((post.get("content") or "").split())[:90]
            ticket = " ".join((row.get("ticket") or "").split())
            why = " ".join((row.get("why") or "").split())
            activity = activity_id(post.get("url") or "", post.get("id") or "")
            thread = list(post.get("comments") or []) + list(comments_by_post.get(activity) or [])
            for side in sides:
                payload = dict(post)
                payload["comments"] = []
                payload["value"] = ticket
                payload["email"] = (row.get("email") or post.get("email") or "").strip()
                payload["phone"] = (row.get("phone") or post.get("phone") or "").strip()
                if len(sides) > 1:
                    payload["id"] = f"{post['id']}-{side}"
                    if side != "buy":
                        payload["url"] = ""
                prefix = "Closed" if state == "closed" else ("Intermediary" if person == "intermediary" else ("Sell" if side == "sell" else "Buy"))
                label = f"{prefix} · {title}"
                result = ingest_post(
                    store, payload, sector, "live",
                    Classification(side, person, 0, 0, int(person == "intermediary"), False, sector, why),
                    state,
                )
                deal_id = result.get("deal_id")
                if not deal_id:
                    continue
                kept += 1
                store._db.execute(
                    "UPDATE nodes SET label=?, short=?, value=?, note=? WHERE id=?",
                    (label, _short(label, 32), ticket, why, deal_id),
                )
                deal = store.get_node(deal_id) or {}
                for comment in thread:
                    if not comment.get("name") or comment["name"].casefold() in {"unknown", (post.get("author_name") or "").casefold()}:
                        continue
                    marker = (deal_id, comment.get("profile") or comment["name"].casefold())
                    if marker in seen_comments:
                        continue
                    seen_comments.add(marker)
                    kept_comments.append({
                        "id": f"{deal_id}:{comment.get('profile') or comment['name']}",
                        "deal_id": deal_id,
                        "deal_side": side,
                        "deal": f"{label}. {why}",
                        "name": comment["name"],
                        "headline": comment.get("headline") or "",
                        "text": comment.get("text") or "",
                        "profile": comment.get("profile") or "",
                    })
                print(f"{person:12} {side:4} {sector:16} {ticket[:18]:18} {post.get('author_name')} — {why}", flush=True)
        from mandate_import import merge_same_labels
        merge_same_labels(store, "sector")
        merge_same_labels(store, "person")
        comment_summary = {"kept": 0}
        if kept_comments:
            from hermes_classify import classify_comments
            comment_cache = ROOT / "data" / "monaco-comment-classes.json"
            comment_verdicts = classify_comments(kept_comments, load_cache(comment_cache), comment_cache)
            comment_summary = apply_comments(store, kept_comments, comment_verdicts)
        store._db.commit()
        print(json.dumps({"posts": len(posts), "kept": kept, "comments": comment_summary, "graph": store.counts()}, indent=2), flush=True)
        store.close()
        return
    if mode == "sellers":
        store = GraphStore(DB)
        max_posts = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        items = search_queries(DIRECT_SELLERS, max_posts)
        append_raw(items)
        posts = []
        seen = set()
        for item in items:
            post = normalize_post(item)
            if not post or not post.get("id") or post["id"] in seen:
                continue
            seen.add(post["id"])
            posts.append(post)
        verdicts = load_cache(CLASSES)
        fresh = [post for post in posts if str(post["id"]) not in verdicts]
        print(f"classifying {len(fresh)} new posts", flush=True)
        fresh_verdicts = classify_posts(fresh) if fresh else {}
        verdicts.update(fresh_verdicts)
        save_cache(CLASSES, verdicts)
        seller_ids = {
            pid for pid, row in fresh_verdicts.items()
            if row.get("keep") and row.get("side") == "sell" and row.get("person_side") == "sell"
        }
        activities = {activity_id(post.get("url") or "", post["id"]) for post in posts if str(post["id"]) in seller_ids}
        kept_items = []
        for item in items:
            post = normalize_post(item)
            if post and str(post.get("id") or "") in seller_ids:
                kept_items.append(item)
                continue
            comment = normalize_comment_item(item)
            if comment and comment.get("post_activity") in activities:
                kept_items.append(item)
        summary = ingest_batch(store, kept_items)
        print(json.dumps({
            "queries": DIRECT_SELLERS,
            "posts": len(posts),
            "direct_sellers": len(seller_ids),
            **summary,
            "graph": store.counts(),
        }, indent=2), flush=True)
        for pid in seller_ids:
            row = fresh_verdicts[pid]
            print(f"seller {row.get('why')}", flush=True)
        store.close()
        return
    max_posts = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    store = GraphStore(DB)
    if mode == "preliminary":
        queries = [query for query, _side in PRELIMINARY]
    elif mode == "seed":
        posts = []
        if RAW.exists():
            for line in RAW.read_text().splitlines():
                item = json.loads(line)
                post = normalize_post(item)
                if post and keep(post):
                    posts.append(post)
        queries = seed_queries(posts)
        print("seed queries:", ", ".join(queries) or "(none)", flush=True)
        if not queries:
            return
    else:
        queries = [part for part in mode.split("|") if part]
    items = search_queries(queries, max_posts)
    append_raw(items)
    summary = ingest_batch(store, items)
    print(json.dumps({"queries": queries, **summary, "graph": store.counts()}, indent=2), flush=True)
    store.close()


if __name__ == "__main__":
    main()
