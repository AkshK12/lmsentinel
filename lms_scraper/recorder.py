"""Recon recorder: logs every network request/response (password redacted) and saves
HTML + screenshots of every step, so we can map PeopleSoft's pages and requests."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import Page, Request, Response

SECRET_FIELDS = re.compile(r"^(pwd|password|lcsrf_token|ICSID)$", re.I)
BODY_TYPES = {"document", "xhr", "fetch"}


def _redact_post(data: str | None) -> dict | str | None:
    if not data:
        return None
    try:
        pairs = parse_qsl(data, keep_blank_values=True, strict_parsing=True)
        return {k: ("***" if SECRET_FIELDS.match(k) else v[:200]) for k, v in pairs}
    except ValueError:
        return re.sub(r"(pwd=)[^&]*", r"\1***", data)[:2000]


class Recorder:
    def __init__(self, out_dir: Path):
        self.dir = out_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
        (self.dir / "bodies").mkdir(parents=True, exist_ok=True)
        self._log = open(self.dir / "requests.jsonl", "w", encoding="utf-8")
        self._n = 0
        self._tasks: set[asyncio.Task] = set()
        self._attached: set[int] = set()
        self._steps: list[str] = []

    def attach(self, page: Page) -> None:
        if id(page) in self._attached:
            return
        self._attached.add(id(page))
        page.on("request", self._on_request)
        page.on("response", lambda r: self._spawn(self._on_response(r)))

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _write(self, entry: dict) -> None:
        self._log.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _on_request(self, req: Request) -> None:
        if req.resource_type not in BODY_TYPES and req.method == "GET":
            return  # skip images/css/js noise
        self._write({"ev": "request", "method": req.method, "type": req.resource_type, "url": req.url,
                     "post": _redact_post(req.post_data)})

    async def _on_response(self, resp: Response) -> None:
        req = resp.request
        if req.resource_type not in BODY_TYPES:
            return
        self._n += 1
        n = self._n
        entry = {"ev": "response", "n": n, "status": resp.status, "method": req.method,
                 "type": req.resource_type, "url": resp.url, "content_type": resp.headers.get("content-type", "")}
        if 200 <= resp.status < 300:
            try:
                body = await resp.text()
                path = self.dir / "bodies" / f"{n:04d}.txt"
                path.write_text(body, encoding="utf-8")
                entry["body_file"] = path.name
            except Exception:
                pass
        self._write(entry)

    async def snapshot(self, page: Page, name: str) -> None:
        await asyncio.sleep(0.3)
        frames = []
        for i, frame in enumerate(page.frames):
            try:
                html = await frame.content()
            except Exception:
                continue
            fname = f"{name}_frame{i}.html"
            (self.dir / fname).write_text(html, encoding="utf-8")
            frames.append({"index": i, "name": frame.name, "url": frame.url, "file": fname})
        try:
            await page.screenshot(path=str(self.dir / f"{name}.png"), full_page=True)
        except Exception:
            pass
        (self.dir / f"{name}_frames.json").write_text(json.dumps(frames, indent=2), encoding="utf-8")
        self._steps.append(f"## {name}\n- url: {page.url}\n- title: {await page.title()}\n"
                           + "".join(f"- frame {f['index']} ({f['name'] or '-'}): {f['url']}\n" for f in frames))

    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._log.close()
        # human-readable summary of the request flow
        lines = []
        for raw in (self.dir / "requests.jsonl").read_text(encoding="utf-8").splitlines():
            e = json.loads(raw)
            if e["ev"] == "response":
                u = urlsplit(e["url"])
                lines.append(f"{e['n']:04d} {e['status']} {e['method']:4} {u.path}{'?' + u.query if u.query else ''}")
        (self.dir / "SUMMARY.md").write_text(
            "# Recon summary\n\n" + "\n".join(self._steps) + "\n\n## Request flow\n```\n" + "\n".join(lines) + "\n```\n",
            encoding="utf-8")
