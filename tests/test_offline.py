"""End-to-end test against a local mock of the PeopleSoft flow.  Run:  python -m tests.test_offline"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright

from lms_scraper.client import LMSClient, LoginError
from lms_scraper.config import Settings
from lms_scraper.notifier import format_changes
from lms_scraper.service import apply_result, deliver_pending
from lms_scraper.store import Account, Store

MARKS = {
    0: [("Quiz 1", "8", "10"), ("Mid Sem", "", "30")],
    1: [("Lab Eval 1", "15", "20")],
}
SUBJECTS = ["UCS301 - Data Structures", "UMA021 - Numerical Analysis"]
VISITS: list[str] = []  # term value submitted each time the subject list is loaded


def page(body: str) -> bytes:
    return f"<html><head><title>t</title></head><body>{body}</body></html>".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, status=200, location=None):
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        ok = data.get("pwd") == ["right"]
        self._send(b"", 302, "/home" if ok else "/psp/csprd/?cmd=login&errorCode=105")

    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if u.path == "/psp/csprd/":
            if q.get("cmd") == ["logout"]:
                return self._send(page("bye"))
            err = "<span>Your User ID and/or Password are invalid.</span>" if "errorCode" in q else ""
            return self._send(page(f'{err}<form id="login" method="post" action="/psp/csprd/?&cmd=login">'
                                   '<input id="userid" name="userid"><input id="pwd" name="pwd" type="password">'
                                   '<input type="submit" name="Submit"></form>'))
        if u.path == "/home":
            return self._send(page('<div><a href="/grades">View My Grades</a></div>'))
        if u.path == "/grades":
            return self._send(page('<a href="/terms">View My Assignments</a>'))
        if u.path == "/terms":
            # rendered 3 s late by JS, like the slow-loading PeopleSoft page seen on GitHub's servers
            form = ('<form action="/classes"><table><tr><th>Select</th><th>Term</th><th>Career</th></tr>'
                    '<tr><td><input type="radio" id="TERM$0" name="t" value="2425EVEN"></td><td>2425 EVEN SEM</td><td>UG</td></tr>'
                    '<tr><td><input type="radio" id="TERM$1" name="t" value="2526ODD"></td><td>2526 ODD SEM</td><td>UG</td></tr>'
                    '</table><input type="submit" value="Continue"></form>')
            return self._send(page(
                '<div id="c">Processing...</div><script>setTimeout(function () {'
                f"document.getElementById('c').innerHTML = {json.dumps(form)}; }}, 3000);</script>"))
        if u.path == "/classes":
            VISITS.append(q.get("t", [""])[0])
            rows = "".join(f'<tr><td><a id="CLASS_LINK${i}" href="/class?i={i}">{s}</a></td><td>LEC</td></tr>'
                           for i, s in enumerate(SUBJECTS))
            return self._send(page(f'<table><tr><th>Class</th><th>Type</th></tr>{rows}</table>'
                                   '<table><tr><td><a id="HELP" href="#">Help</a></td></tr></table>'))
        if u.path == "/class":
            i = int(q["i"][0])
            rows = "".join(f"<tr><td>{a}</td><td>{s}</td><td>{m}</td></tr>" for a, s, m in MARKS[i])
            return self._send(page(f'<h1>{SUBJECTS[i]}</h1><table id="ASSIGN$scroll$0">'
                                   f'<tr><th>Assignment</th><th>Score</th><th>Max Points</th></tr>{rows}</table>'
                                   '<a href="#">Return</a>'  # broken, like the real site
                                   '<input type="button" value="Change Class" onclick="location=\'/terms\'">'))
        self._send(b"not found", 404)


async def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    settings = dataclasses.replace(Settings(), base_url=base, assignments_url="", timeout_ms=8000,
                                   term_strategy="last", subject_link_regex="")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "t.db")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        async def scrape(pwd="right"):
            async with LMSClient(browser, settings) as c:
                return await c.scrape("student@thapar.edu", pwd)

        # 1. bad password -> LoginError
        try:
            await scrape("wrong")
            raise AssertionError("expected LoginError")
        except LoginError as e:
            print("OK  bad password rejected:", e)

        # 2. baseline (latest = last term; back-navigation via Change Class re-selects the same term)
        VISITS.clear()
        r = await scrape()
        assert VISITS == ["2526ODD", "2526ODD"], VISITS
        print("OK  latest term chosen, Change Class path re-selected it:", VISITS)
        assert r.term.startswith("2526 ODD SEM"), r.term
        assert [s.name for s in r.subjects] == SUBJECTS, [s.name for s in r.subjects]
        assert r.subjects[0].grids[0]["rows"][0] == {"Assignment": "Quiz 1", "Score": "8", "Max Points": "10"}
        assert apply_result(store, "student", r) == []
        print("OK  baseline:", r.term, [s.name for s in r.subjects])

        # 3. no change
        assert apply_result(store, "student", await scrape()) == []
        print("OK  unchanged run reports nothing")

        # 4. marks uploaded + new assignment
        MARKS[0][1] = ("Mid Sem", "24", "30")
        MARKS[1].append(("Lab Eval 2", "18", "20"))
        changes = apply_result(store, "student", await scrape())
        for c in changes:
            print("    ", c.to_text())
        kinds = sorted((c.kind, c.item) for c in changes)
        assert kinds == [("added", "Lab Eval 2"), ("modified", "Mid Sem")], kinds
        assert len(store.unnotified()) == 2
        print("OK  changes detected and logged")

        # 5. delivery: a failed send keeps changes pending; the next successful send clears them
        class FakeNotifier:
            def __init__(self, ok):
                self.ok, self.got = ok, []

            async def notify(self, account, term, ch):
                self.got += ch
                return self.ok

        acc = Account("student", "")
        await deliver_pending(store, acc, FakeNotifier(False))
        assert len(store.unnotified("student")) == 2
        good = FakeNotifier(True)
        await deliver_pending(store, acc, good)
        assert len(good.got) == 2 and not store.unnotified("student")
        msg = format_changes(r.term, good.got)
        assert len(msg) == 1 and "<b>UCS301 - Data Structures</b>" in msg[0] and "Mid Sem" in msg[0]
        print("OK  failed Telegram send is retried; message:\n" + msg[0])

        # 6. simulate-change: the next real check reports exactly that one value and restores it
        subject, item, field = store.simulate_change("student")
        changes = apply_result(store, "student", await scrape())
        assert [(c.kind, c.subject, c.item) for c in changes] == [("modified", subject, item)], changes
        assert changes[0].fields[0][1] == "(simulated old value)"
        assert apply_result(store, "student", await scrape()) == []
        print("OK  simulate-change:", changes[0].to_text())
        await browser.close()
    srv.shutdown()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
