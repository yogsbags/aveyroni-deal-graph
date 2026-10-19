"""Turn Apify LinkedIn post items into the fields the graph stores.

harvestapi/linkedin-post-search does not publish a stable output schema.
The normalizer accepts the field names that actor has used, plus a nested
comment list when scrapeComments is on.
"""

from __future__ import annotations

import re
from typing import Any

EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE = re.compile(r"(?:\+\d{1,3}[\s.-])?\(?\d{2,4}\)?[\s.-]\d{3,4}[\s.-]\d{3,4}")
_CUR = {
    "$": "USD", "usd": "USD", "us$": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR",
    "aed": "AED",
    "¥": "JPY", "jpy": "JPY", "yen": "JPY",
}
_UNIT = {
    "k": 1e3,
    "m": 1e6, "mn": 1e6, "million": 1e6, "millions": 1e6,
    "bn": 1e9, "b": 1e9, "billion": 1e9, "billions": 1e9,
    "cr": 1e7, "crore": 1e7, "crores": 1e7,
}
_MONEY = re.compile(
    r"(?P<cur>₹|rs\.?|inr|usd|us\$|\$|eur|€|gbp|£|aed|jpy|¥|yen)?"
    r"\s*(?P<a>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
    r"(?:\s*(?:-|–|—|to)\s*(?P<cur2>₹|rs\.?|inr|usd|us\$|\$|eur|€|gbp|£|aed|jpy|¥|yen)?"
    r"\s*(?P<b>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?))?"
    r"\s*(?P<unit>crores?|cr|millions?|mn|billions?|bn|m|k)?"
    r"\b",
    re.I,
)
_SKIP_AFTER = re.compile(
    r"\s*(?:%|percent|key|keys|room|rooms|guest|guests|mw\b|sq\b|bigha|/month|/guntha)",
    re.I,
)


def _num(raw: str) -> float:
    return float(raw.replace(",", ""))


def _currency(token: str | None) -> str:
    return _CUR.get((token or "").lower(), "")


def money_spans(text: str) -> list[dict]:
    """Ticket amounts in major units. Rates, room counts and guest totals are left out."""
    if not text:
        return []
    spans = []
    for match in _MONEY.finditer(text):
        tail = text[match.end():match.end() + 20]
        if _SKIP_AFTER.match(tail) or re.match(r"\s*\+?\s*(?:p\.a\.|per annum|annually)(?!\w)", tail, re.I):
            continue
        unit = (match.group("unit") or "").lower()
        scale = _UNIT.get(unit, 1.0)
        currency = _currency(match.group("cur")) or _currency(match.group("cur2"))
        if unit in {"cr", "crore", "crores"}:
            currency = currency or "INR"
        if unit in {"m", "mn"} and not currency:
            continue
        if not unit and not currency:
            continue
        if unit in {"billion", "billions", "bn", "b"} and not currency:
            window = text[max(0, match.start() - 40):match.end() + 12]
            if re.search(r"jpy|yen|¥", window, re.I):
                currency = "JPY"
            elif re.search(r"aed", window, re.I):
                currency = "AED"
            elif re.search(r"usd|\$|eur|€|gbp|£", window, re.I):
                currency = "USD" if re.search(r"usd|\$", window, re.I) else (
                    "EUR" if re.search(r"eur|€", window, re.I) else "GBP"
                )
            else:
                continue
        low = _num(match.group("a")) * (scale if unit else 1.0)
        high = _num(match.group("b")) * (scale if unit else 1.0) if match.group("b") else low
        if not unit:
            if currency == "INR" and max(low, high) < 1e5:
                continue
            if currency != "INR" and max(low, high) < 1000:
                continue
        if high < low:
            low, high = high, low
        if high <= 0 or not currency:
            continue
        if low <= 0:
            low = high
        spans.append({"currency": currency, "low": low, "high": high})
    return spans


def _trim(number: float) -> str:
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return text


def money_phrase(text: str) -> str:
    spans = money_spans(text)
    if not spans:
        return ""
    grouped: dict[str, list[float]] = {}
    for span in spans:
        grouped.setdefault(span["currency"], []).append(span["low"])
        grouped[span["currency"]].append(span["high"])
    parts = []
    for currency, values in grouped.items():
        low, high = min(values), max(values)
        parts.append(_format_span(low, high, currency))
    return " · ".join(parts)


def _format_span(low: float, high: float, currency: str) -> str:
    def one(amount: float) -> str:
        if currency == "INR":
            return f"₹{_trim(amount / 1e7)} Cr"
        symbol = {"USD": "$", "EUR": "€", "GBP": "£", "AED": "AED ", "JPY": "¥"}.get(currency, "")
        if amount >= 1e9:
            return f"{symbol}{_trim(amount / 1e9)}bn"
        if amount >= 1e6:
            return f"{symbol}{_trim(amount / 1e6)}M"
        if amount >= 1e3:
            return f"{symbol}{_trim(amount / 1e3)}k"
        return f"{symbol}{_trim(amount)}"
    if abs(high - low) / max(high, 1) < 0.02:
        return one(high)
    return f"{one(low)}–{one(high)}"


def _first(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _author(raw: Any) -> dict[str, str]:
    if isinstance(raw, str) and raw.strip():
        return {"name": raw.strip(), "headline": "", "profile": ""}
    if not isinstance(raw, dict):
        return {"name": "", "headline": "", "profile": ""}
    name = _first(raw, "name", "fullName", "authorName", "author_name")
    headline = _first(raw, "info", "headline", "title", "occupation", "position", "author_headline")
    profile = _first(raw, "linkedinUrl", "profileUrl", "authorProfileUrl", "profile", "author_profile", "url")
    return {"name": name, "headline": headline, "profile": profile}


def _posted(item: dict) -> str:
    posted = item.get("postedAt") or item.get("postedDate") or item.get("posted") or item.get("date")
    if isinstance(posted, str):
        return posted.strip()
    if isinstance(posted, dict):
        return _first(posted, "date", "postedAgoText", "postedAgoShort")
    return ""


def _comments(item: dict) -> list[dict[str, str]]:
    raw = item.get("comments") or item.get("nestedComments") or []
    if not isinstance(raw, list):
        return []
    out = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        text = _first(row, "text", "content", "commentary", "comment")
        author = _author(row.get("author") or row.get("commenter") or row)
        if not text and not author["name"]:
            continue
        out.append({"text": text, "name": author["name"], "headline": author["headline"], "profile": author["profile"]})
    return out


def is_comment_item(item: dict) -> bool:
    kind = str(item.get("type") or item.get("resultType") or "").lower()
    return kind == "comment" or (bool(item.get("comment")) and not item.get("content") and not item.get("commentary"))


def normalize_post(item: dict) -> dict | None:
    if not isinstance(item, dict) or is_comment_item(item):
        return None
    content = _first(item, "content", "text", "commentary", "description")
    url = _first(item, "linkedinUrl", "url", "postUrl", "link")
    author = _author(item.get("author") or item.get("actor") or {
        "name": item.get("authorName") or item.get("author_name"),
        "headline": item.get("authorHeadline") or item.get("author_headline"),
        "profile": item.get("authorProfileUrl") or item.get("author_profile"),
    })
    if not content and not url:
        return None
    email = EMAIL.search(content)
    phone = PHONE.search(content)
    value = money_phrase(content)
    return {
        "id": _first(item, "id", "urn", "activityId"),
        "url": url,
        "content": content,
        "posted": _posted(item),
        "author_name": author["name"] or "Unknown",
        "author_headline": author["headline"],
        "author_profile": author["profile"],
        "email": email.group(0) if email else "",
        "phone": phone.group(0) if phone else "",
        "value": value,
        "comments": _comments(item),
        "query": _first(item, "query", "searchQuery", "search"),
    }


def activity_id(*values: str) -> str:
    for value in values:
        match = re.search(r"(?:activity[:\-]|urn:li:activity:)(\d+)", value or "")
        if match:
            return match.group(1)
    return ""


def normalize_comment_item(item: dict) -> dict | None:
    if not is_comment_item(item):
        return None
    text = _first(item, "text", "content", "commentary", "comment")
    author = _author(item.get("author") or item.get("actor") or item.get("commenter") or {})
    post_activity = activity_id(str(item.get("postId") or ""), _first(item, "postUrl", "parentUrl", "linkedinUrl", "url"))
    post_url = _first(item, "postUrl", "parentUrl")
    if not post_url and post_activity:
        post_url = f"https://www.linkedin.com/feed/update/urn:li:activity:{post_activity}"
    if not text or not (post_url or post_activity):
        return None
    return {
        "text": text,
        "name": author["name"] or "Unknown",
        "headline": author["headline"],
        "profile": author["profile"],
        "post_url": post_url,
        "post_activity": post_activity,
    }
