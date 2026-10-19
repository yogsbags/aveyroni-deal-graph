"""Segregate a LinkedIn post into the deal graph's four sides.

The graph filters deals and people as direct sellers, direct buyers,
intermediaries, or other signals. Classification is scored from the post
text and the author's headline so a banker posting a sale stays an
intermediary while the deal itself stays sell-side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SELL = [
    (4, r"\b(business|company|firm) for sale\b"),
    (4, r"\b(seeking|looking for) (a )?buyer\b"),
    (4, r"\b(looking|want|wants|ready) to sell\b"),
    (3, r"\bsell[- ]side (mandate|advisor|adviser|process|teaser|engagement)\b"),
    (3, r"\b(divestiture|divesting|carve[- ]out|strategic exit)\b"),
    (3, r"\b(confidential (sale|teaser)|information memorandum|\bcim\b)\b"),
    (2, r"\b(100%|majority) (stake|equity|shareholding).{0,24}(sale|available|offered)\b"),
    (2, r"\b(exit (opportunity|process)|teaser available)\b"),
    (2, r"\boff[- ]market (sale|mandate|opportunity)\b"),
    (3, r"\b(hotel|resort|hospitality asset).{0,48}(for sale|on the market|available for (sale|acquisition))\b"),
    (2, r"\b(keys|operating hotel|leasehold hotel) for sale\b"),
]
BUY = [
    (4, r"\b(looking|seeking) to acquire\b"),
    (4, r"\bacquisition mandate\b"),
    (4, r"\bbuy[- ]side\b"),
    (3, r"\b(add-on|bolt[- ]on|platform) acquisition\b"),
    (3, r"\b(seeking|searching for) (targets|acquisitions|a platform)\b"),
    (3, r"\b(actively|currently) acquiring\b"),
    (2, r"\bacquisition criteria\b"),
    (2, r"\b(mandate to (buy|acquire)|buy[- ]side search)\b"),
    (2, r"\bwe (are|'re) (buying|acquiring)\b"),
    (3, r"\b(looking|seeking) to (acquire|buy).{0,40}\b(hotel|resort|hospitality)\b"),
    (3, r"\b(hotel|resort|hospitality) (portfolio|platform|asset).{0,24}(acqui|buy)\b"),
]
INTERMEDIARY = [
    (4, r"\b(m&a|mergers and acquisitions) (advisor|adviser|advisory)\b"),
    (3, r"\b(investment bank|boutique advisory|corporate finance boutique)\b"),
    (3, r"\b(on behalf of (our |a )?(client|seller|buyer)|representing (a |our )?(client|seller))\b"),
    (3, r"\b(sell-side|buy-side) (advisor|adviser)\b"),
    (2, r"\b(advising|advisor|adviser) on the (sale|acquisition|disposal)\b"),
    (2, r"\b(nda required|teaser on request|contact (me|us) for the teaser)\b"),
    (2, r"\b(managing director|partner|vice president).{0,40}(advisory|investment bank)\b"),
]
DEALISH = re.compile(
    r"acqui|merger|\bm&a\b|mandate|divest|for sale|\bbuyer\b|\bseller\b|teaser|\bcim\b|carve[- ]out",
    re.I,
)
HIRING = re.compile(
    r"\b(we are hiring|we're hiring|now hiring|job opening|we(?:'re| are) recruiting|looking for a|we're looking for|join (?:our|the) team|is hiring|talent acquisition|#hiring)\b",
    re.I,
)

SECTORS = [
    ("Hospitality", re.compile(r"\bhotels?\b|\bresorts?\b|\bhospitality\b|\blodging\b|\bboutique inn\b|\bserviced apartment", re.I)),
    ("Healthcare", re.compile(r"hospital|pharma|clinic|healthcare|medtech|diagnostic", re.I)),
    ("Software", re.compile(r"\bsaas\b|software|devtools|cybersecurity", re.I)),
    ("Manufacturing", re.compile(r"manufactur|industrial|auto component|foundry|machining|engineering company", re.I)),
    ("Real estate", re.compile(r"real estate|warehouse|property portfolio|\breit\b", re.I)),
    ("Energy", re.compile(r"renewable|solar|oil and gas|\benergy\b", re.I)),
    ("Consumer", re.compile(r"\bconsumer\b|\bfmcg\b|\bd2c\b|retail brand", re.I)),
    ("Financial services", re.compile(r"\bnbfc\b|insurance|asset management|fintech|wealth", re.I)),
    ("Logistics", re.compile(r"logistic|freight|supply chain", re.I)),
    ("Business services", re.compile(r"\bbpo\b|staffing|facilities management", re.I)),
]


@dataclass(frozen=True)
class Classification:
    side: str
    person_side: str
    sell: int
    buy: int
    intermediary: int
    skip: bool
    sector: str
    why: str


def _score(rules: list[tuple[int, str]], text: str) -> int:
    total = 0
    for weight, pattern in rules:
        if re.search(pattern, text, re.I):
            total += weight
    return total


def infer_sector(text: str, hint: str = "") -> str:
    for name, pattern in SECTORS:
        if pattern.search(text):
            return name
    hint = (hint or "").strip()
    return hint or "Unsorted"


def classify(text: str, headline: str = "", sector_hint: str = "") -> Classification:
    body = (text or "").strip()
    head = (headline or "").strip()
    blob = f"{head}\n{body}".strip()
    sell = _score(SELL, blob)
    buy = _score(BUY, blob)
    mid = _score(INTERMEDIARY, f"{head}\n{body}")
    dealish = bool(DEALISH.search(blob))
    hiring = bool(HIRING.search(blob))

    if hiring and sell == 0 and buy == 0:
        return Classification("signal", "signal", sell, buy, mid, True, infer_sector(blob, sector_hint), "hiring, not a deal")
    if sell == 0 and buy == 0 and mid == 0 and not dealish:
        return Classification("signal", "signal", 0, 0, 0, True, infer_sector(blob, sector_hint), "no deal language")

    if sell > buy and sell > 0:
        side = "sell"
    elif buy > sell and buy > 0:
        side = "buy"
    elif sell == buy and sell > 0:
        side = "sell" if mid >= 2 else "signal"
    elif mid >= 2:
        side = "intermediary"
    else:
        side = "signal"

    if mid >= 2 and side in {"sell", "buy"}:
        person_side = "intermediary"
    else:
        person_side = side

    why = f"sell {sell} · buy {buy} · intermediary {mid}"
    return Classification(side, person_side, sell, buy, mid, False, infer_sector(blob, sector_hint), why)
