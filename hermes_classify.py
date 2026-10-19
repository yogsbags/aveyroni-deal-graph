"""Ask the local Hermes agent which hospitality posts are live deals.

Hermes sees the post text and the author's headline. It decides buy side,
sell side, or drop. Regex only extracts the JSON it returns.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

HERMES = "hermes"
BATCH = 12

PROMPT = """You are classifying LinkedIn posts for an M&A desk. Only live hospitality deals belong on the graph.

A post is a deal when a real party is offering a hotel portfolio, resort, or whole multi-key hotel for sale, is actively looking to buy or acquire one, or is announcing a deal that has already closed. Include the closed announcement. A single apartment, flat, or one hotel room is not a hospitality deal. The poster and the people who commented are part of the deal network.

Set state to "closed" when the transaction is already done and nothing in the post is still for sale or still being acquired. Set state to "open" when a buyer, seller, or advisor still has a live mandate or listing. Drop hiring, student exercises, supply ads, and commentary that names no transaction.

For the author:
- intermediary: broker, advisor, banker, or consultant representing someone else
- sell: the owner or principal selling
- buy: the principal buying
- signal: not a party to a live deal

Return ONLY a JSON array. No markdown. Each item:
{"id":"<id>","keep":true|false,"state":"open"|"closed"|"drop","side":"sell"|"buy"|"skip","person_side":"sell"|"buy"|"intermediary"|"signal","why":"one sentence"}

Posts:
"""


def _extract_json(text: str) -> list[dict]:
    fenced = re.search(r"```(?:json)?\s*(\[.*\])\s*```", text, re.S)
    raw = fenced.group(1) if fenced else text
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end < start:
        raise ValueError(text[:400])
    data = json.loads(raw[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("Hermes did not return a list")
    return [row for row in data if isinstance(row, dict)]


CLOSED_PROMPT = """You are looking only for closed hospitality deal announcements. These posts were previously set aside.

Keep a post when it announces a hotel, resort, or hospitality sale or acquisition that has already happened, and it names the asset, the firm, or the people involved. The point is the network: who posted, and who showed up in the comments.

Drop hiring, student exercises, essays, market statistics, supply ads, and brokerage ads that name no completed transaction.

Return ONLY a JSON array. No markdown. Each item:
{"id":"<id>","state":"closed"|"drop","side":"sell"|"buy"|"skip","person_side":"sell"|"buy"|"intermediary"|"signal","why":"one sentence"}

side is sell when an asset was sold, buy when someone acquired it.

Posts:
"""


def _ask(prompt: str) -> list[dict]:
    completed = subprocess.run(
        [
            HERMES, "-z", prompt,
            "--ignore-rules",
            "--reasoning", "low",
            "--provider", "openai-codex",
            "-m", "gpt-5.6-luna",
        ],
        capture_output=True,
        text=True,
        timeout=240,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:500]
        raise RuntimeError(f"Hermes exited {completed.returncode}: {detail}")
    return _extract_json(completed.stdout)


def classify_batch(posts: list[dict]) -> list[dict]:
    payload = []
    for post in posts:
        payload.append({
            "id": post["id"],
            "author": post.get("author_name") or "",
            "headline": (post.get("author_headline") or "")[:240],
            "text": (post.get("content") or "")[:1200],
        })
    prompt = PROMPT + json.dumps(payload, ensure_ascii=False)
    return _ask(prompt)


def classify_closed(posts: list[dict]) -> list[dict]:
    payload = []
    for post in posts:
        payload.append({
            "id": post["id"],
            "author": post.get("author_name") or "",
            "headline": (post.get("author_headline") or "")[:240],
            "text": (post.get("content") or "")[:1200],
        })
    return _ask(CLOSED_PROMPT + json.dumps(payload, ensure_ascii=False))


def classify_posts(posts: list[dict]) -> dict[str, dict]:
    """Return id -> Hermes verdict. Posts need id, content, author_name, author_headline."""
    found: dict[str, dict] = {}
    for offset in range(0, len(posts), BATCH):
        chunk = posts[offset : offset + BATCH]
        print(f"hermes {offset + 1}-{offset + len(chunk)} of {len(posts)}", flush=True)
        for row in classify_batch(chunk):
            if row.get("id"):
                found[str(row["id"])] = row
    return found


MANDATE_PROMPT = """You are classifying mandate rows for an M&A deal graph. Each row is one LinkedIn post, already summarized. The sheet's own side and category are hints. You decide.

Keep a row when a real party is buying, selling, or mandating a specific asset, company, or stated buy box. Include a closed announcement. Drop hiring, essays, and posts that name no mandate and no asset.

Asset rule: keep hotel portfolios, resorts, and whole hotels (multi-key, boutique, palace, or a hotel business). Keep commercial property when the premises or the business in them is the asset, and entire residential property only when the whole villa, château, building, or estate is the asset. Drop a single residential unit: one apartment, flat, condo, penthouse, unit number, or any stated bedroom count such as 1BHK, 2BHK, one-bedroom, or three-bedroom apartment. Drop a brand, domain, or trademark with no property. Drop a post that only uses Monaco as a price comparison. A hotel room or a single key is not a hotel.

side is the deal: "sell" when an asset or company is for sale or was sold, "buy" when someone is acquiring or seeking to acquire, "both" only when that same post is genuinely running a buy mandate and a separate sell mandate.
person_side is the author: "intermediary" when they represent a client, buyer, seller, or fund; "sell" when they are the owner; "buy" when they are the principal deploying their own capital; "signal" when they are not a party.
sector is one short category. Prefer: Hospitality, Real estate, Healthcare, Education, Financial services, Defence & technology, Gaming, Renewable energy, Business services, Consumer, Software, Media, SME debt & working capital. Invent a short name only when none of those fit.
ticket is the asking price, purchase budget, or equity cheque only, as a compact string such as €7M, ₹50 Cr–₹500 Cr, £2M–£20M, or $70M–$500M. Leave ticket empty when the only figures are turnover, revenue, EBITDA, profit, loan book, or fees.
label is a short deal title, at most 14 words, with no row numbers and no "the sheet says".
email and phone are copied from the contact field when they are actually there, otherwise empty.

Return ONLY a JSON array. No markdown. Each item:
{"id":"<id>","keep":true|false,"state":"open"|"closed"|"drop","side":"sell"|"buy"|"both"|"skip","person_side":"sell"|"buy"|"intermediary"|"signal","sector":"...","ticket":"...","label":"...","email":"...","phone":"...","why":"one sentence"}

Rows:
"""


INDIA_PROMPT = """You are classifying LinkedIn posts for an India sell-side round. Keep a post only when a specific operating business in India is for sale or has been sold, and that business is manufacturing, industrials, or an NBFC. Cashflowing means it already operates: the post mentions revenue, EBITDA, profit, cash flow, a running plant, a loan book, or a going concern. Include a closed sale of such a business.

Drop hiring, essays, buy-side searches, land, single apartments, hotels, pre-revenue startups, and posts where India is only a sign-off or one name in a country list while the business itself is elsewhere. Drop a brand, domain, or trademark with no operating company.

person_side is "sell" only when the author is the owner, promoter, or principal of that business. "intermediary" when a broker or advisor is selling it for someone else. "signal" when they are reporting the news and are not a party.
sector is Manufacturing, Industrials, or Financial services. Use Financial services for an NBFC.
side is "sell" when the business is offered or was sold. Do not use "buy".
ticket is the asking price or equity cheque only, such as ₹50 Cr or ₹20 Cr–₹80 Cr. Leave ticket empty when the only figures are turnover, revenue, EBITDA, profit, or the loan book.
label is a short deal title, at most 14 words.
why is one sentence and says the business is in India.
email and phone are copied only when the post actually contains them, otherwise empty.

Return ONLY a JSON array. No markdown. Each item:
{"id":"<id>","keep":true|false,"state":"open"|"closed"|"drop","side":"sell"|"buy"|"both"|"skip","person_side":"sell"|"buy"|"intermediary"|"signal","sector":"...","ticket":"...","label":"...","email":"...","phone":"...","why":"one sentence"}

Rows:
"""


def classify_mandates(posts: list[dict], cache: dict[str, dict] | None = None, cache_path: Path | None = None, prompt: str | None = None) -> dict[str, dict]:
    """Classify mandate rows. Posts need id, content, author_name, author_headline, and may include sheet_side and sheet_category."""
    instructions = prompt or MANDATE_PROMPT
    found = dict(cache or {})
    pending = [post for post in posts if str(post["id"]) not in found]
    for offset in range(0, len(pending), BATCH):
        chunk = pending[offset : offset + BATCH]
        print(f"hermes {offset + 1}-{offset + len(chunk)} of {len(pending)}", flush=True)
        payload = []
        for post in chunk:
            payload.append({
                "id": post["id"],
                "author": post.get("author_name") or "",
                "headline": (post.get("author_headline") or "")[:240],
                "sheet_side": post.get("sheet_side") or "",
                "sheet_category": post.get("sheet_category") or "",
                "contact": (post.get("contact") or "")[:160],
                "text": (post.get("content") or "")[:1400],
            })
        try:
            rows = _ask(instructions + json.dumps(payload, ensure_ascii=False))
        except (RuntimeError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as err:
            print(f"batch failed ({err}); splitting", flush=True)
            rows = []
            for item in payload:
                rows.extend(_ask(instructions + json.dumps([item], ensure_ascii=False)))
        for row in rows:
            if row.get("id"):
                found[str(row["id"])] = row
        if cache_path is not None:
            save_cache(cache_path, found)
    return found


COMMENT_PROMPT = """You are classifying people who commented on an M&A or deal post. Decide whether the commenter is a relevant party to deal-making.

person_side:
- buy: a principal who wants to acquire, has capital, or states their own buy mandate
- sell: an owner or principal offering an asset or company
- intermediary: a broker, advisor, banker, or consultant representing a buyer, seller, or mandate
- signal: not a party

relevant is false for congratulations, emoji, "#cfbr", "please DM" with no role, the author's own reply, job seekers, and self-promotion that is not a buy, sell, or mandate. relevant is true only for buy, sell, or intermediary.

Return ONLY a JSON array. No markdown. Each item:
{"id":"<id>","relevant":true|false,"person_side":"sell"|"buy"|"intermediary"|"signal","why":"one sentence"}

Comments:
"""


def classify_comments(comments: list[dict], cache: dict[str, dict] | None = None, cache_path: Path | None = None) -> dict[str, dict]:
    """Classify commenters. Each comment needs id, deal, deal_side, name, headline, text."""
    found = dict(cache or {})
    pending = [row for row in comments if str(row["id"]) not in found]
    for offset in range(0, len(pending), BATCH):
        chunk = pending[offset : offset + BATCH]
        print(f"hermes comments {offset + 1}-{offset + len(chunk)} of {len(pending)}", flush=True)
        payload = [{
            "id": row["id"],
            "deal_side": row.get("deal_side") or "",
            "deal": (row.get("deal") or "")[:280],
            "name": row.get("name") or "",
            "headline": (row.get("headline") or "")[:200],
            "text": (row.get("text") or "")[:400],
        } for row in chunk]
        try:
            rows = _ask(COMMENT_PROMPT + json.dumps(payload, ensure_ascii=False))
        except (RuntimeError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as err:
            print(f"comment batch failed ({err}); splitting", flush=True)
            rows = []
            for item in payload:
                rows.extend(_ask(COMMENT_PROMPT + json.dumps([item], ensure_ascii=False)))
        for row in rows:
            if row.get("id"):
                found[str(row["id"])] = row
        if cache_path is not None:
            save_cache(cache_path, found)
    return found


def load_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else {}


def save_cache(path: Path, verdicts: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdicts, indent=2, ensure_ascii=False))
