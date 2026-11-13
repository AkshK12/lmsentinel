"""Hashing + row-level diffing of marks grids."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

KEY_HINTS = ("assignment", "description", "item", "title", "name", "component", "evaluation", "exam", "test")


def hash_grids(grids: list[dict]) -> str:
    canon = json.dumps([{"h": g["headers"], "r": g["rows"]} for g in grids], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


@dataclass
class Change:
    subject: str
    kind: str  # added | removed | modified | new_subject
    item: str
    before: dict | None = None
    after: dict | None = None
    fields: list[tuple[str, str, str]] = field(default_factory=list)

    def describe(self) -> str:
        if self.kind == "new_subject":
            return "new subject appeared"
        if self.kind == "added":
            vals = ", ".join(f"{k}: {v}" for k, v in (self.after or {}).items() if v)
            return f"NEW '{self.item}' -> {vals}"
        if self.kind == "removed":
            return f"removed '{self.item}'"
        diffs = "; ".join(f"{f}: '{o}' -> '{n}'" for f, o, n in self.fields)
        return f"UPDATED '{self.item}' -> {diffs}"

    def to_text(self) -> str:
        return f"[{self.subject}] {self.describe()}"

    def to_dict(self) -> dict:
        return asdict(self)


def _key_col(headers: list[str]) -> str:
    for h in headers:
        if any(k in h.lower() for k in KEY_HINTS):
            return h
    return headers[0]


def _index(grids: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for gi, g in enumerate(grids):
        gid = g.get("id") or g.get("title") or str(gi)
        kc = _key_col(g["headers"])
        for row in g["rows"]:
            base = f"{gid}|{row.get(kc, '')}"
            key, n = base, 1
            while key in out:  # duplicate item names
                n += 1
                key = f"{base}#{n}"
            out[key] = row
    return out


def diff_grids(subject: str, old: list[dict], new: list[dict]) -> list[Change]:
    o, n = _index(old), _index(new)
    changes: list[Change] = []
    for key, row in n.items():
        label = key.split("|", 1)[1]
        if key not in o:
            changes.append(Change(subject, "added", label, after=row))
        elif o[key] != row:
            fields = [(f, o[key].get(f, ""), row.get(f, ""))
                      for f in sorted(set(o[key]) | set(row)) if o[key].get(f, "") != row.get(f, "")]
            changes.append(Change(subject, "modified", label, before=o[key], after=row, fields=fields))
    for key, row in o.items():
        if key not in n:
            changes.append(Change(subject, "removed", key.split("|", 1)[1], before=row))
    return changes
