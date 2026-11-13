"""Local SQLite storage (on the student's own laptop): latest snapshot per subject + change log."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    user_id TEXT NOT NULL,
    term TEXT NOT NULL,
    subject TEXT NOT NULL,
    hash TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, term, subject)
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    term TEXT NOT NULL,
    subject TEXT NOT NULL,
    kind TEXT NOT NULL,
    item TEXT,
    detail TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Account:
    user_id: str
    password: str


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("DROP TABLE IF EXISTS users")  # from the old shared-credentials design

    # ------------------------------------------------------------ snapshots
    def has_any_snapshot(self, user_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM snapshots WHERE user_id=? LIMIT 1", (user_id,)).fetchone() is not None

    def get_snapshot(self, user_id: str, term: str, subject: str) -> tuple[str, list[dict]] | None:
        r = self.db.execute("SELECT hash, data FROM snapshots WHERE user_id=? AND term=? AND subject=?",
                            (user_id, term, subject)).fetchone()
        return (r["hash"], json.loads(r["data"])) if r else None

    def save_snapshot(self, user_id: str, term: str, subject: str, h: str, grids: list[dict]) -> None:
        self.db.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?) ON CONFLICT(user_id, term, subject) "
            "DO UPDATE SET hash=excluded.hash, data=excluded.data, updated_at=excluded.updated_at",
            (user_id, term, subject, h, json.dumps(grids, ensure_ascii=False), now()),
        )
        self.db.commit()

    def simulate_change(self, user_id: str) -> tuple[str, str, str] | None:
        """Testing aid: alter one value in the SAVED copy so the next real check reports
        it as a change (and restores the true value). Returns (subject, item, field)."""
        from .diff import _key_col

        for r in self.db.execute("SELECT term, subject, data FROM snapshots WHERE user_id=?", (user_id,)).fetchall():
            grids = json.loads(r["data"])
            for g in grids:
                if not g["rows"] or len(g["headers"]) < 2:
                    continue
                key = _key_col(g["headers"])
                field = next(h for h in reversed(g["headers"]) if h != key)
                row = g["rows"][-1]
                row[field] = "(simulated old value)"
                self.db.execute("UPDATE snapshots SET hash='simulated', data=? WHERE user_id=? AND term=? AND subject=?",
                                (json.dumps(grids, ensure_ascii=False), user_id, r["term"], r["subject"]))
                self.db.commit()
                return r["subject"], row.get(key, ""), field
        return None

    def latest(self, user_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT term, subject, data, updated_at FROM snapshots WHERE user_id=? "
                               "ORDER BY term, subject", (user_id,)).fetchall()

    # --------------------------------------------------------------- changes
    def log_changes(self, user_id: str, term: str, changes) -> None:
        self.db.executemany(
            "INSERT INTO changes(user_id, term, subject, kind, item, detail, detected_at) VALUES(?,?,?,?,?,?,?)",
            [(user_id, term, c.subject, c.kind, c.item, json.dumps(c.to_dict(), ensure_ascii=False), now())
             for c in changes],
        )
        self.db.commit()

    def unnotified(self, user_id: str | None = None) -> list[sqlite3.Row]:
        """Changes not yet delivered (e.g. Telegram was down): retried on every check."""
        if user_id is None:
            return self.db.execute("SELECT * FROM changes WHERE notified=0 ORDER BY id").fetchall()
        return self.db.execute("SELECT * FROM changes WHERE notified=0 AND user_id=? ORDER BY id",
                               (user_id,)).fetchall()

    def mark_notified(self, ids: list[int]) -> None:
        self.db.executemany("UPDATE changes SET notified=1 WHERE id=?", [(i,) for i in ids])
        self.db.commit()
