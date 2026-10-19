"""SQLite graph of sectors, deals, people, and the links between them.

Nodes and edges use the same fields the deal canvas reads: kind, side,
label, seq, and rel. Existing ids are kept, so a later scan only appends
what the network has not seen.
"""

from __future__ import annotations

import csv
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT_ID = "aveyroni"

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  side TEXT,
  label TEXT NOT NULL,
  short TEXT,
  sub TEXT,
  seq INTEGER NOT NULL,
  posted TEXT,
  value TEXT,
  url TEXT,
  profile TEXT,
  degree TEXT,
  phone TEXT,
  email TEXT,
  status TEXT,
  note TEXT,
  state TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
  id TEXT PRIMARY KEY,
  from_id TEXT NOT NULL,
  to_id TEXT NOT NULL,
  rel TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  key TEXT PRIMARY KEY,
  url TEXT,
  side TEXT,
  sector_id TEXT,
  deal_id TEXT,
  ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
  id TEXT PRIMARY KEY,
  query TEXT,
  state TEXT,
  message TEXT,
  new_deals INTEGER DEFAULT 0,
  started_at TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GraphStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(nodes)")}
        if "state" not in columns:
            self._db.execute("ALTER TABLE nodes ADD COLUMN state TEXT")
        self._db.commit()
        self.ensure_root()

    def close(self) -> None:
        self._db.close()

    def root_id(self) -> str:
        row = self._db.execute("SELECT id FROM nodes WHERE kind='root' ORDER BY seq LIMIT 1").fetchone()
        return row["id"] if row else ROOT_ID

    def ensure_root(self) -> dict | None:
        if self._db.execute("SELECT 1 FROM nodes WHERE kind='root'").fetchone():
            return None
        return self.upsert_node({
            "id": ROOT_ID,
            "kind": "root",
            "label": "Aveyroni",
            "short": "Aveyroni",
            "sub": "Deal network",
            "status": "live",
        })

    def _next_seq(self) -> int:
        row = self._db.execute("SELECT v FROM meta WHERE k='seq'").fetchone()
        seq = int(row["v"]) + 1 if row else 1
        self._db.execute(
            "INSERT INTO meta(k,v) VALUES('seq',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(seq),),
        )
        return seq

    def get_node(self, node_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return _node(row) if row else None

    def seen_post(self, key: str) -> bool:
        return self._db.execute("SELECT 1 FROM posts WHERE key=?", (key,)).fetchone() is not None

    def upsert_node(self, data: dict) -> dict | None:
        """Insert a node. Return it when it is new. Refresh text fields when it already exists."""
        with self._lock:
            current = self._db.execute("SELECT * FROM nodes WHERE id=?", (data["id"],)).fetchone()
            if current:
                side = _keep_side(current["side"], data.get("side"))
                self._db.execute(
                    """UPDATE nodes SET label=?, short=?, sub=?, posted=?, value=?, url=?, profile=?,
                       phone=?, email=?, status=?, note=?, side=?, state=COALESCE(?, state)
                       WHERE id=?""",
                    (
                        data.get("label") or current["label"],
                        data.get("short") or current["short"],
                        data.get("sub") or current["sub"],
                        data.get("posted") or current["posted"],
                        data.get("value") or current["value"],
                        data.get("url") or current["url"],
                        data.get("profile") or current["profile"],
                        data.get("phone") or current["phone"],
                        data.get("email") or current["email"],
                        data.get("status") or current["status"],
                        data.get("note") or current["note"],
                        side,
                        data.get("state"),
                        data["id"],
                    ),
                )
                self._db.commit()
                return None
            seq = self._next_seq()
            created = _now()
            self._db.execute(
                """INSERT INTO nodes(id,kind,side,label,short,sub,seq,posted,value,url,profile,degree,phone,email,status,note,state,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    data["id"], data["kind"], data.get("side"), data["label"], data.get("short"),
                    data.get("sub"), seq, data.get("posted"), data.get("value"), data.get("url"),
                    data.get("profile"), data.get("degree"), data.get("phone"), data.get("email"),
                    data.get("status"), data.get("note"), data.get("state"), created,
                ),
            )
            self._db.commit()
            row = self._db.execute("SELECT * FROM nodes WHERE id=?", (data["id"],)).fetchone()
            return _node(row)

    def add_edge(self, from_id: str, to_id: str, rel: str, note: str = "") -> dict | None:
        edge_id = f"{rel}:{from_id}:{to_id}"
        with self._lock:
            if self._db.execute("SELECT 1 FROM edges WHERE id=?", (edge_id,)).fetchone():
                return None
            if not self._db.execute("SELECT 1 FROM nodes WHERE id=?", (from_id,)).fetchone():
                return None
            if not self._db.execute("SELECT 1 FROM nodes WHERE id=?", (to_id,)).fetchone():
                return None
            self._db.execute(
                "INSERT INTO edges(id,from_id,to_id,rel,note,created_at) VALUES(?,?,?,?,?,?)",
                (edge_id, from_id, to_id, rel, note or None, _now()),
            )
            self._db.commit()
            return {"id": edge_id, "from": from_id, "to": to_id, "rel": rel, "note": note or None}

    def remember_post(self, key: str, url: str, side: str, sector_id: str, deal_id: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO posts(key,url,side,sector_id,deal_id,ingested_at) VALUES(?,?,?,?,?,?)",
                (key, url, side, sector_id, deal_id, _now()),
            )
            self._db.commit()

    def deal_for_url(self, url: str) -> str | None:
        row = self._db.execute("SELECT deal_id FROM posts WHERE url=?", (url,)).fetchone()
        return row["deal_id"] if row else None

    def opposite_deals(self, sector_id: str, side: str, limit: int = 3) -> list[str]:
        opposite = "buy" if side == "sell" else "sell" if side == "buy" else ""
        if not opposite:
            return []
        rows = self._db.execute(
            """SELECT DISTINCT n.id FROM nodes n
               JOIN edges e ON (e.from_id=n.id OR e.to_id=n.id)
               WHERE n.kind='deal' AND n.side=? AND e.rel='sector'
                 AND (e.from_id=? OR e.to_id=?)
               ORDER BY n.seq DESC LIMIT ?""",
            (opposite, sector_id, sector_id, limit),
        ).fetchall()
        return [row["id"] for row in rows]

    def recent_profiles(self, limit: int = 8) -> list[str]:
        rows = self._db.execute(
            """SELECT profile FROM nodes
               WHERE kind='person' AND profile LIKE 'http%'
               ORDER BY seq DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        seen = []
        for row in rows:
            if row["profile"] not in seen:
                seen.append(row["profile"])
        return seen

    def snapshot(self) -> dict:
        nodes = [_node(row) for row in self._db.execute("SELECT * FROM nodes ORDER BY seq")]
        edges = [_edge(row) for row in self._db.execute("SELECT * FROM edges ORDER BY created_at")]
        return {"nodes": nodes, "edges": edges}

    def counts(self) -> dict:
        row = self._db.execute(
            """SELECT
                 SUM(kind='deal') AS deals,
                 SUM(kind='person' AND side='sell') AS sell,
                 SUM(kind='person' AND side='buy') AS buy,
                 SUM(kind='person' AND side='intermediary') AS intermediary,
                 SUM(kind='person' AND side='signal') AS signal
               FROM nodes"""
        ).fetchone()
        matches = self._db.execute("SELECT COUNT(*) AS n FROM edges WHERE rel='match'").fetchone()["n"]
        return {
            "deals": row["deals"] or 0,
            "sell": row["sell"] or 0,
            "buy": row["buy"] or 0,
            "intermediary": row["intermediary"] or 0,
            "signal": row["signal"] or 0,
            "matches": matches,
            "posts": self._db.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"],
        }

    def replace_from_csv(self, nodes_path: Path, edges_path: Path) -> dict:
        """Replace the stored network with a canvas export. Later scans append after it."""
        with self._lock:
            now = _now()
            with nodes_path.open(newline="", encoding="utf-8") as handle:
                nodes = list(csv.DictReader(handle))
            with edges_path.open(newline="", encoding="utf-8") as handle:
                edges = list(csv.DictReader(handle))
            self._db.execute("DELETE FROM edges")
            self._db.execute("DELETE FROM nodes")
            self._db.execute("DELETE FROM posts")
            for row in nodes:
                seq = int(row["seq"]) if row.get("seq") else self._next_seq()
                self._db.execute(
                    """INSERT INTO nodes(id,kind,side,label,short,sub,seq,posted,value,url,profile,degree,phone,email,status,note,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["id"], row["kind"], _blank(row.get("side")), row["label"], _blank(row.get("short")),
                        _blank(row.get("sub")), seq, _blank(row.get("posted")), _blank(row.get("value")),
                        _blank(row.get("url")), _blank(row.get("profile")), _blank(row.get("degree")),
                        _blank(row.get("phone")), _blank(row.get("email")), _blank(row.get("status")),
                        _blank(row.get("note")), now,
                    ),
                )
            kept = 0
            for row in edges:
                if not self._db.execute("SELECT 1 FROM nodes WHERE id=?", (row["from"],)).fetchone():
                    continue
                if not self._db.execute("SELECT 1 FROM nodes WHERE id=?", (row["to"],)).fetchone():
                    continue
                self._db.execute(
                    "INSERT OR REPLACE INTO edges(id,from_id,to_id,rel,note,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], row["from"], row["to"], row["rel"], _blank(row.get("note")), now),
                )
                kept += 1
            top = self._db.execute("SELECT COALESCE(MAX(seq),0) AS n FROM nodes").fetchone()["n"]
            self._db.execute(
                "INSERT INTO meta(k,v) VALUES('seq',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(top),),
            )
            self._db.commit()
        return {"nodes": len(nodes), "edges": kept}

    def start_scan(self, scan_id: str, query: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO scans(id,query,state,message,new_deals,started_at) VALUES(?,?,?,?,0,?)",
                (scan_id, query, "running", "scanning LinkedIn", _now()),
            )
            self._db.commit()

    def finish_scan(self, scan_id: str, state: str, message: str, new_deals: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE scans SET state=?, message=?, new_deals=?, finished_at=? WHERE id=?",
                (state, message, new_deals, _now(), scan_id),
            )
            self._db.commit()


def _blank(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _keep_side(current: str | None, incoming: str | None) -> str | None:
    rank = {"signal": 0, "intermediary": 1, "sell": 2, "buy": 2}
    if rank.get(incoming or "", -1) >= rank.get(current or "", -1):
        return incoming or current
    return current


def _node(row: sqlite3.Row) -> dict:
    data = {
        "id": row["id"],
        "kind": row["kind"],
        "label": row["label"],
        "seq": row["seq"],
    }
    for key in ("side", "short", "sub", "posted", "value", "url", "profile", "degree", "phone", "email", "status", "note", "state"):
        if row[key]:
            data[key] = row[key]
    return data


def _edge(row: sqlite3.Row) -> dict:
    data = {"id": row["id"], "from": row["from_id"], "to": row["to_id"], "rel": row["rel"]}
    if row["note"]:
        data["note"] = row["note"]
    return data
