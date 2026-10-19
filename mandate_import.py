"""Add mandate-sheet rows using Hermes, not pattern rules.

Hermes decides keep, deal side, whether the author is a principal or an
intermediary, the sector, and the ticket. The sheet columns are hints.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from classify import Classification
from hermes_classify import classify_mandates, load_cache
from ingest import _short, ingest_post, person_id, post_key
from store import GraphStore

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "graph.db"
CACHE = ROOT / "data" / "mandate-classes.json"
SHEET_DIR = ROOT / "data" / "sheets"
COLUMNS = [
    "num", "author", "headline", "category", "side", "posted", "summary", "full",
    "contact", "reactions", "comment_count", "commenters", "query", "url", "match", "match_url",
]


def usable(text: str) -> str:
    value = " ".join((text or "").split())
    if value.lower().startswith("not captured") or value.lower() in {"n/a", "n/a - not retained"}:
        return ""
    return value


def clip(text: str, limit: int = 90) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut


def load_headerless(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(zip(COLUMNS, row)) for row in csv.reader(handle)]


def load_headed(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = []
        for row in csv.DictReader(handle):
            rows.append({
                "num": row.get("#") or "",
                "author": row.get("Post Author") or "",
                "headline": row.get("Author Title/Headline") or "",
                "category": row.get("Mandate Category") or "",
                "side": row.get("Buy-Side / Sell-Side") or "",
                "posted": row.get("Posted (relative/date)") or "",
                "summary": row.get("Mandate Summary (Buy Box / Criteria)") or "",
                "full": row.get("Full Post Text (excerpt)") or "",
                "contact": row.get("Contact Info Given") or "",
                "url": row.get("Post URL") or "",
            })
        return rows


def as_post(row: dict) -> dict:
    url = usable(row.get("url") or "")
    if not url.startswith("http"):
        url = ""
    summary = usable(row.get("summary") or "")
    full = usable(row.get("full") or "")
    headline = usable(row.get("headline") or "")
    return {
        "id": f"sheet-{row['num'].strip()}",
        "url": url,
        "content": "\n".join(part for part in (summary, full) if part),
        "summary": summary,
        "posted": (row.get("posted") or "").strip(),
        "author_name": (row.get("author") or "").strip(),
        "author_headline": headline,
        "author_profile": "",
        "sheet_side": (row.get("side") or "").strip(),
        "sheet_category": (row.get("category") or "").strip(),
        "contact": usable(row.get("contact") or ""),
        "comments": [],
    }


def titled(person_side: str, side: str, title: str, state: str) -> str:
    title = title or "Mandate"
    if state == "closed":
        return "Closed · " + title
    if person_side == "intermediary":
        return "Intermediary · " + title
    if side == "sell":
        return "Sell · " + title
    if side == "buy":
        return "Buy · " + title
    return title


def candidate_keys(post: dict) -> list[str]:
    keys = [post_key(post)]
    for suffix in ("", "-buy", "-sell"):
        twin = dict(post, id=post["id"] + suffix, url="" if suffix else post.get("url"))
        if suffix:
            twin["url"] = ""
        keys.append(post_key(twin))
    seen = []
    for key in keys:
        if key not in seen:
            seen.append(key)
    return seen


def drop_deal(store: GraphStore, deal_id: str) -> None:
    store._db.execute("DELETE FROM edges WHERE from_id=? OR to_id=?", (deal_id, deal_id))
    store._db.execute("DELETE FROM nodes WHERE id=? AND kind='deal'", (deal_id,))
    store._db.execute("DELETE FROM posts WHERE deal_id=?", (deal_id,))


def move_sector(store: GraphStore, deal_id: str, sector: str) -> str:
    sectors = {
        (row["label"] or "").casefold(): row["id"]
        for row in store._db.execute("SELECT id, label FROM nodes WHERE kind='sector'")
    }
    sector_id = sectors.get(sector.casefold())
    if not sector_id:
        from ingest import _slug
        sector_id = _slug("sector", sector)
        store.upsert_node({
            "id": sector_id,
            "kind": "sector",
            "label": sector,
            "short": sector,
            "status": "live",
        })
        store.add_edge(store.root_id(), sector_id, "sector", "")
    store._db.execute("DELETE FROM edges WHERE rel='sector' AND to_id=?", (deal_id,))
    store.add_edge(sector_id, deal_id, "sector", sector)
    return sector_id


def apply_verdict(store: GraphStore, post: dict, verdict: dict) -> str:
    keep = bool(verdict.get("keep")) and verdict.get("state") != "drop" and verdict.get("side") in {"sell", "buy", "both"}
    db = store._db
    found = {}
    for key in candidate_keys(post):
        row = db.execute("SELECT deal_id, side FROM posts WHERE key=?", (key,)).fetchone()
        if row and row["deal_id"]:
            found[row["side"] or ""] = row["deal_id"]
    if post.get("url"):
        row = db.execute(
            "SELECT id, side FROM nodes WHERE kind='deal' AND url=?",
            (post["url"],),
        ).fetchone()
        if row:
            found.setdefault(row["side"] or "", row["id"])
    if not keep:
        for deal_id in set(found.values()):
            drop_deal(store, deal_id)
        return "dropped" if found else "skipped"

    sides = ["buy", "sell"] if verdict.get("side") == "both" else [verdict.get("side")]
    person = verdict.get("person_side") if verdict.get("person_side") in {"sell", "buy", "intermediary", "signal"} else "intermediary"
    state = "closed" if verdict.get("state") == "closed" else "open"
    sector = (verdict.get("sector") or "Business services").strip()
    title = clip(verdict.get("label") or post.get("summary") or "")
    ticket = " ".join((verdict.get("ticket") or "").split())
    why = " ".join((verdict.get("why") or "").split())
    email = (verdict.get("email") or "").strip()
    phone = (verdict.get("phone") or "").strip()
    kept_ids = set()
    for side in sides:
        deal_id = found.get(side)
        label = titled(person, side, title, state)
        if deal_id and db.execute("SELECT 1 FROM nodes WHERE id=?", (deal_id,)).fetchone():
            db.execute(
                """UPDATE nodes SET side=?, label=?, short=?, value=?, note=?, state=?, status=?, posted=?
                   WHERE id=?""",
                (
                    side, label, _short(label, 32), ticket, why, state,
                    "Closed" if state == "closed" else "live", post.get("posted") or "", deal_id,
                ),
            )
            sector_id = move_sector(store, deal_id, sector)
            if side != found.get(side) or True:
                db.execute("DELETE FROM edges WHERE rel='match' AND (from_id=? OR to_id=?)", (deal_id, deal_id))
                if state != "closed":
                    for other in store.opposite_deals(sector_id, side):
                        store.add_edge(deal_id, other, "match", "same sector, opposite side")
            kept_ids.add(deal_id)
            continue
        twin = dict(post)
        twin["id"] = post["id"] + ("" if len(sides) == 1 else f"-{side}")
        if len(sides) > 1 and side != "buy":
            twin["url"] = ""
        twin["value"] = ticket
        twin["email"] = email
        twin["phone"] = phone
        twin["content"] = title or post.get("summary") or post.get("content")
        result = ingest_post(
            store, twin, sector, "live",
            Classification(side, person, 0, 0, int(person == "intermediary"), False, sector, why),
            state,
        )
        if result.get("deal_id"):
            deal_id = result["deal_id"]
            db.execute(
                "UPDATE nodes SET label=?, short=?, value=?, note=? WHERE id=?",
                (label, _short(label, 32), ticket, why, deal_id),
            )
            move_sector(store, deal_id, sector)
            kept_ids.add(deal_id)
    for side, deal_id in found.items():
        if deal_id not in kept_ids:
            drop_deal(store, deal_id)
    author = person_id(post.get("author_name") or "", "")
    if db.execute("SELECT 1 FROM nodes WHERE id=?", (author,)).fetchone():
        current = db.execute("SELECT email, phone, sub FROM nodes WHERE id=?", (author,)).fetchone()
        if email and not current["email"]:
            db.execute("UPDATE nodes SET email=? WHERE id=?", (email, author))
        if phone and not current["phone"]:
            db.execute("UPDATE nodes SET phone=? WHERE id=?", (phone, author))
        if post.get("author_headline") and not current["sub"]:
            db.execute("UPDATE nodes SET sub=? WHERE id=?", (post["author_headline"], author))
    return "kept"


def merge_same_labels(store: GraphStore, kind: str) -> int:
    db = store._db
    groups: dict[str, list] = {}
    for row in db.execute("SELECT id, label, seq, email, phone, sub FROM nodes WHERE kind=?", (kind,)):
        groups.setdefault((row["label"] or "").casefold(), []).append(row)
    moved = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda row: row["seq"] or 0)
        keep = rows[0]
        for extra in rows[1:]:
            if kind == "person":
                if extra["email"] and not keep["email"]:
                    db.execute("UPDATE nodes SET email=? WHERE id=?", (extra["email"], keep["id"]))
                if extra["phone"] and not keep["phone"]:
                    db.execute("UPDATE nodes SET phone=? WHERE id=?", (extra["phone"], keep["id"]))
            edges = db.execute("SELECT * FROM edges WHERE from_id=? OR to_id=?", (extra["id"], extra["id"])).fetchall()
            for edge in edges:
                src = keep["id"] if edge["from_id"] == extra["id"] else edge["from_id"]
                dst = keep["id"] if edge["to_id"] == extra["id"] else edge["to_id"]
                edge_id = f"{edge['rel']}:{src}:{dst}"
                if src != dst and not db.execute("SELECT 1 FROM edges WHERE id=?", (edge_id,)).fetchone():
                    db.execute(
                        "INSERT INTO edges(id,from_id,to_id,rel,note,created_at) VALUES(?,?,?,?,?,?)",
                        (edge_id, src, dst, edge["rel"], edge["note"] or "", edge["created_at"]),
                    )
                db.execute("DELETE FROM edges WHERE id=?", (edge["id"],))
            db.execute("DELETE FROM nodes WHERE id=?", (extra["id"],))
            moved += 1
    return moved


def set_person_sides(store: GraphStore, posts: list[dict], verdicts: dict[str, dict]) -> None:
    by_name: dict[str, set[str]] = {}
    for post in posts:
        verdict = verdicts.get(post["id"]) or {}
        if not verdict.get("keep") or verdict.get("side") not in {"sell", "buy", "both"}:
            continue
        side = verdict.get("person_side")
        if side not in {"sell", "buy", "intermediary", "signal"}:
            continue
        by_name.setdefault(post["author_name"].casefold(), set()).add(side)
    for name, sides in by_name.items():
        concrete = sides - {"signal"}
        if concrete == {"buy"}:
            chosen = "buy"
        elif concrete == {"sell"}:
            chosen = "sell"
        elif "intermediary" in concrete or not concrete:
            chosen = "intermediary" if "intermediary" in sides or not concrete else "signal"
        else:
            chosen = "intermediary"
        if not concrete:
            chosen = "signal"
        store._db.execute(
            "UPDATE nodes SET side=? WHERE kind='person' AND lower(label)=lower(?)",
            (chosen, name),
        )


def main() -> None:
    SHEET_DIR.mkdir(parents=True, exist_ok=True)
    sources = [
        ("mandates.csv", ROOT / "data" / "mandates.csv", True),
        ("sheet-a.csv", Path("/tmp/deal-sheets/a.bin"), False),
        ("sheet-b.csv", Path("/tmp/deal-sheets/b.bin"), False),
        ("sheet-c.csv", Path("/tmp/deal-sheets/c.bin"), False),
    ]
    posts = []
    seen = set()
    for name, path, headed in sources:
        target = SHEET_DIR / name
        if path.exists() and path.resolve() != target.resolve():
            shutil.copy(path, target)
        rows = load_headed(target) if headed else load_headerless(target)
        for row in rows:
            post = as_post(row)
            if not post["author_name"] or not post["content"] or post["id"] in seen:
                continue
            seen.add(post["id"])
            posts.append(post)
    print(f"rows {len(posts)}", flush=True)
    verdicts = classify_mandates(posts, load_cache(CACHE), CACHE)
    store = GraphStore(DB)
    counts = {"kept": 0, "dropped": 0, "skipped": 0}
    for post in posts:
        verdict = verdicts.get(post["id"])
        if not verdict:
            print(f"missing verdict {post['id']}", flush=True)
            continue
        result = apply_verdict(store, post, verdict)
        counts[result] = counts.get(result, 0) + 1
    people = merge_same_labels(store, "person")
    sectors = merge_same_labels(store, "sector")
    set_person_sides(store, posts, verdicts)
    orphans = store._db.execute(
        """SELECT id FROM nodes WHERE kind='person' AND status='live'
           AND id NOT IN (SELECT from_id FROM edges UNION SELECT to_id FROM edges)"""
    ).fetchall()
    for orphan in orphans:
        store._db.execute("DELETE FROM nodes WHERE id=?", (orphan["id"],))
    store._db.commit()
    print({"rows": len(posts), **counts, "people_merged": people, "sectors_merged": sectors, "graph": store.counts()}, flush=True)
    store.close()


if __name__ == "__main__":
    main()
