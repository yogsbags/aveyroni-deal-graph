#!/usr/bin/env python3
"""Serve the deal graph and grow it from LinkedIn post search.

The canvas reads /api/graph once, then /api/stream. Each new post becomes a
sector (if needed), a deal, the author, and any commenters. Repeat scans
skip posts already stored, so the network only grows.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from apify_source import ApifyError, search_posts
from ingest import attach_comment, ingest_post
from normalize import normalize_comment_item, normalize_post
from store import GraphStore

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static" / "index.html"
FIXTURES = ROOT / "fixtures" / "sample_posts.json"
QUERIES = ROOT / "queries.json"
DB_PATH = Path(os.environ.get("DEAL_GRAPH_DB") or (ROOT / "data" / "graph.db"))
HOST = os.environ.get("HOST") or "127.0.0.1"
PORT = int(os.environ.get("PORT") or "8792")

store = GraphStore(DB_PATH)
scan_lock = threading.Lock()


class Hub:
    def __init__(self) -> None:
        self.seq = 0
        self.recent: deque[tuple[int, str]] = deque(maxlen=400)
        self.clients: list[deque] = []
        self.lock = threading.Lock()

    def publish(self, event: str, data: dict) -> None:
        with self.lock:
            self.seq += 1
            msg = f"id: {self.seq}\nevent: {event}\ndata: {json.dumps(data)}\n\n"
            self.recent.append((self.seq, msg))
            stale = []
            for client in self.clients:
                if len(client) > 200:
                    stale.append(client)
                    continue
                client.append(msg)
            for client in stale:
                self.clients.remove(client)

    def snapshot(self) -> dict:
        with self.lock:
            payload = store.snapshot()
            payload["cursor"] = self.seq
            payload["counts"] = store.counts()
            return payload

    def subscribe(self, after: int) -> deque:
        client: deque = deque()
        with self.lock:
            for event_id, msg in self.recent:
                if event_id > after:
                    client.append(msg)
            self.clients.append(client)
        return client

    def unsubscribe(self, client: deque) -> None:
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)


hub = Hub()


def _emit(result: dict) -> None:
    for node in result.get("nodes") or []:
        hub.publish("node", node)
    for edge in result.get("edges") or []:
        hub.publish("edge", edge)


def ingest_items(items: list[dict], sector_hint: str = "", status: str = "live", pause: float = 0) -> int:
    new_deals = 0
    for item in items:
        comment = normalize_comment_item(item)
        if comment:
            _emit(attach_comment(store, comment.pop("post_url"), comment, status))
            continue
        post = normalize_post(item)
        if not post:
            continue
        hint = sector_hint or post.get("query") or ""
        result = ingest_post(store, post, hint, status)
        if result.get("side") in {"sell", "buy", "intermediary", "signal"} and not result.get("skipped"):
            new_deals += 1
        _emit(result)
        if pause:
            time.sleep(pause)
    return new_deals


def load_queries() -> dict:
    if not QUERIES.exists():
        return {"queries": [], "max_posts": 12, "posted_limit": "month", "scrape_comments": False, "follow_authors": 0}
    return json.loads(QUERIES.read_text())


def run_scan(body: dict) -> None:
    scan_id = uuid.uuid4().hex[:12]
    config = load_queries()
    single = (body.get("query") or "").strip()
    watchlist = bool(body.get("watchlist"))
    if single:
        jobs = [{"query": single, "sector": (body.get("sector") or "").strip()}]
    elif watchlist:
        jobs = config.get("queries") or []
    else:
        hub.publish("status", {"state": "idle", "message": "add a LinkedIn search"})
        return
    max_posts = int(body.get("max_posts") or config.get("max_posts") or 12)
    posted_limit = body.get("posted_limit") or config.get("posted_limit") or "month"
    scrape_comments = bool(body.get("scrape_comments", config.get("scrape_comments", False)))
    follow = int(body.get("follow_authors") if body.get("follow_authors") is not None else (config.get("follow_authors") or 0) if watchlist else 0)
    label = single or "watchlist"
    store.start_scan(scan_id, label)
    hub.publish("status", {"state": "scanning", "message": "scanning LinkedIn"})
    new_deals = 0
    try:
        if not scan_lock.acquire(blocking=False):
            raise ApifyError("A scan is already running.")
        try:
            for job in jobs:
                items = search_posts(
                    queries=[job["query"]],
                    max_posts=max_posts,
                    posted_limit=posted_limit,
                    scrape_comments=scrape_comments,
                )
                new_deals += ingest_items(items, job.get("sector") or "", "live")
            if follow:
                profiles = store.recent_profiles(follow)
                if profiles:
                    hub.publish("status", {"state": "scanning", "message": f"following {len(profiles)} people"})
                    items = search_posts(author_urls=profiles, max_posts=min(5, max_posts), posted_limit="week", scrape_comments=False)
                    new_deals += ingest_items(items, "", "live")
        finally:
            scan_lock.release()
        message = f"live · {new_deals} new" if new_deals else "live · nothing new"
        store.finish_scan(scan_id, "done", message, new_deals)
        hub.publish("status", {"state": "live", "message": message})
    except ApifyError as exc:
        store.finish_scan(scan_id, "error", str(exc), new_deals)
        hub.publish("status", {"state": "error", "message": str(exc)})


def load_sample() -> int:
    posts = json.loads(FIXTURES.read_text())
    return ingest_items(posts, "", "sample", pause=0.35)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def end_headers(self) -> None:
        self.send_header("access-control-allow-origin", "*")
        super().end_headers()

    def _send(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        length = int(self.headers.get("content-length") or 0)
        if not length:
            return {}
        data = json.loads(self.rfile.read(length).decode() or "{}")
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:
        path = urlparse(self.path)
        if path.path in {"/", "/index.html"}:
            raw = STATIC.read_bytes()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path.path == "/api/graph":
            self._send(200, hub.snapshot())
            return
        if path.path == "/api/health":
            self._send(200, {"ok": True, **store.counts()})
            return
        if path.path == "/api/stream":
            after = int((parse_qs(path.query).get("after") or ["0"])[0] or 0)
            client = hub.subscribe(after)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            try:
                while True:
                    while client:
                        self.wfile.write(client.popleft().encode())
                        self.wfile.flush()
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    time.sleep(2)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                hub.unsubscribe(client)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/demo":
            threading.Thread(target=load_sample, daemon=True).start()
            self._send(202, {"ok": True, "message": "playing sample posts"})
            return
        if path == "/api/scan":
            body = self._read_json()
            threading.Thread(target=run_scan, args=(body,), daemon=True).start()
            self._send(202, {"ok": True, "message": "scan started"})
            return
        self._send(404, {"error": "not found"})


def _poll() -> None:
    minutes = float(os.environ.get("DEAL_GRAPH_POLL_MINUTES") or 0)
    if minutes <= 0:
        return
    while True:
        time.sleep(minutes * 60)
        run_scan({"watchlist": True})


def main() -> None:
    if "--demo" in sys.argv:
        load_sample()
    threading.Thread(target=_poll, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Deal graph at http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
