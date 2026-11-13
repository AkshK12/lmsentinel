"""Playwright-driven PeopleSoft client: login -> View My Grades -> View My Assignments
-> latest term -> every subject -> extract the marks grid(s)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass

from playwright.async_api import Browser, Locator, Page
from playwright.async_api import TimeoutError as PWTimeout

from .config import Settings
from .extract import GRIDS_JS, LINKS_JS, TERMS_JS

log = logging.getLogger(__name__)

# Link texts that are PeopleSoft chrome, never subjects.
NAV_BLACKLIST = re.compile(
    r"^(return|back|home|help|sign ?out|view all|first|last|previous|next|personali[sz]e|find|"
    r"download|change( term)?|go|search|select|show|hide|details?|\d+|\W*)$",
    re.I,
)


class LoginError(Exception):
    """Bad credentials / locked account. Never retried automatically (avoids lockouts)."""


class NavigationError(Exception):
    """Expected page element not found — the site layout probably changed."""


@dataclass
class SubjectResult:
    name: str
    grids: list[dict]


@dataclass
class ScrapeResult:
    term: str
    subjects: list[SubjectResult]


class LMSClient:
    def __init__(self, browser: Browser, settings: Settings, recorder=None):
        self.browser = browser
        self.s = settings
        self.recorder = recorder
        self.context = None
        self.page: Page | None = None
        self.home_url = ""
        self.term: str | None = None

    async def __aenter__(self) -> "LMSClient":
        self.context = await self.browser.new_context(viewport={"width": 1366, "height": 900})
        self.context.set_default_timeout(self.s.timeout_ms)
        self.page = await self.context.new_page()
        if self.recorder:
            self.recorder.attach(self.page)
        return self

    async def __aexit__(self, *exc) -> None:
        try:  # free the server-side PeopleSoft session
            await self.page.goto(self.s.logout_url, timeout=15000)
        except Exception:
            pass
        await self.context.close()

    # ------------------------------------------------------------------ helpers
    async def _snap(self, name: str) -> None:
        if self.recorder:
            await self.recorder.snapshot(self.page, name)

    async def _settle(self) -> None:
        """Wait for loads + PeopleSoft's 'Processing...' overlay to disappear."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        for frame in self.page.frames:
            try:
                await frame.wait_for_selector("#processing, #WAIT_win0", state="hidden", timeout=5000)
            except Exception:
                pass

    @staticmethod
    async def _first_visible(loc: Locator) -> Locator | None:
        try:
            n = min(await loc.count(), 10)
            for i in range(n):
                if await loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception:
            pass
        return None

    async def _find(self, pattern: str, timeout_ms: int | None = None) -> Locator:
        """Find a clickable element whose accessible name / text matches `pattern`, in any frame."""
        rx = re.compile(pattern, re.I)
        deadline = time.monotonic() + (timeout_ms or self.s.timeout_ms) / 1000
        while True:
            for frame in self.page.frames:
                for role in ("link", "button", "tab", "menuitem"):
                    if found := await self._first_visible(frame.get_by_role(role, name=rx)):
                        return found
                if found := await self._first_visible(frame.get_by_text(rx)):
                    return found
            if time.monotonic() > deadline:
                raise NavigationError(f"No element matching /{pattern}/ on {self.page.url}")
            await asyncio.sleep(0.75)

    async def _click(self, loc: Locator) -> None:
        before = len(self.context.pages)
        await loc.click()
        await asyncio.sleep(0.5)
        if len(self.context.pages) > before:  # link opened a new tab
            self.page = self.context.pages[-1]
            if self.recorder:
                self.recorder.attach(self.page)
            await self.page.wait_for_load_state()
        await self._settle()

    async def _locate_id(self, element_id: str) -> Locator | None:
        for frame in self.page.frames:
            loc = frame.locator(f'[id="{element_id}"]')
            try:
                if await loc.count():
                    return loc.first
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------ steps
    async def login(self, userid: str, password: str) -> None:
        await self.page.goto(self.s.login_url)
        await self._snap("01_login_page")
        await self.page.fill("#userid", userid)
        await self.page.fill("#pwd", password)
        try:
            async with self.page.expect_navigation():
                await self.page.press("#pwd", "Enter")
        except PWTimeout:
            pass
        await self._settle()
        if "cmd=login" in self.page.url or await self.page.locator("#pwd").count():
            body = await self.page.inner_text("body")
            msg = next((ln.strip() for ln in body.splitlines()
                        if re.search(r"invalid|locked|disabled|expired|incorrect", ln, re.I)), "")
            raise LoginError(msg or "Login failed (still on sign-in page)")
        self.home_url = self.page.url
        await self._snap("02_home")
        log.info("Logged in")

    async def open_assignments(self) -> None:
        if self.s.assignments_url:
            await self.page.goto(self.s.assignments_url)
            await self._settle()
        else:
            await self._click(await self._find(self.s.nav_grades))
            await self._snap("03_view_my_grades")
            await self._click(await self._find(self.s.nav_assignments))
        await self._snap("04_assignments_term_page")

    async def _term_options(self):
        for frame in self.page.frames:
            try:
                options = await frame.evaluate(TERMS_JS)
            except Exception:
                continue
            if options:
                return frame, options
        return None, []

    async def select_term(self, same_as: str | None = None) -> str:
        """Pick the term per TERM_STRATEGY, or exactly `same_as` (used when coming back via Change Class).
        Pages can load slowly (e.g. on GitHub's servers), so keep looking until the term list
        or the subject list shows up."""
        start = time.monotonic()
        deadline = start + self.s.timeout_ms / 1000
        while True:
            frame, options = await self._term_options()
            if options or time.monotonic() > deadline:
                break
            # no term step on this page? (grace period so a still-visible previous page doesn't fool us)
            if time.monotonic() - start > 5 and await self._subject_links():
                break
            await asyncio.sleep(1)
        if options:
            chosen = next((o for o in options if o["label"] == same_as), None) if same_as else None
            chosen = chosen or self._choose_term(options)
            log.info("Selecting term: %s", chosen["label"])
            if chosen["kind"] == "radio":
                target = frame.locator(f'[id="{chosen["id"]}"]') if chosen["id"] \
                    else frame.locator("input[type=radio]").nth(chosen["index"])
                await target.check(force=True)
            else:
                await frame.locator(f'[id="{chosen["id"]}"]').select_option(chosen["value"])
            await self._click(await self._find(self.s.nav_continue))
            return chosen["label"]
        log.info("No term selector found; assuming the current term is already shown")
        return "current"

    def _choose_term(self, options: list[dict]) -> dict:
        strat = self.s.term_strategy.strip()
        if strat.lower() == "first":
            return options[0]
        if strat.lower() == "last":
            return options[-1]
        rx = re.compile(strat, re.I)
        for o in options:
            if rx.search(o["label"]):
                return o
        raise NavigationError(f"No term matches TERM_STRATEGY={strat!r}: {[o['label'] for o in options]}")

    async def list_subjects(self, wait_ms: int = 20000) -> list[dict]:
        deadline = time.monotonic() + wait_ms / 1000
        while True:
            subjects = await self._subject_links()
            if subjects or time.monotonic() > deadline:
                break
            await asyncio.sleep(1)
        if subjects:
            log.info("Found %d subject(s): %s", len(subjects), [l["text"] for l in subjects])
        return subjects

    async def describe_page(self) -> str:
        """Page structure for debugging: URLs, counts and button/link labels, never marks."""
        lines = [f"url={self.page.url} title={await self.page.title()!r}"]
        for i, frame in enumerate(self.page.frames):
            try:
                info = await frame.evaluate(r"""() => ({
                    radios: document.querySelectorAll('input[type=radio]').length,
                    selects: document.querySelectorAll('select').length,
                    tables: document.querySelectorAll('table').length,
                    tableLinks: document.querySelectorAll('table a[id]').length,
                    labels: [...document.querySelectorAll('a, button, input[type=button], input[type=submit]')]
                      .filter(e => e.offsetWidth || e.offsetHeight)
                      .map(e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim().slice(0, 40))
                      .filter(Boolean).slice(0, 25),
                })""")
            except Exception as e:
                info = f"<unreadable: {e.__class__.__name__}>"
            lines.append(f"frame {i} name={frame.name!r} url={frame.url[:150]} {info}")
        return "\n".join(lines)

    async def _subject_links(self) -> list[dict]:
        """Subject links = the largest family of PeopleSoft grid links (ids like CLASS_NAME$0, $1 ...)."""
        links: list[dict] = []
        for frame in self.page.frames:
            try:
                links += await frame.evaluate(LINKS_JS)
            except Exception:
                continue
        links = [l for l in links if not NAV_BLACKLIST.match(l["text"])]
        if self.s.subject_link_regex:
            rx = re.compile(self.s.subject_link_regex, re.I)
            return [l for l in links if rx.search(l["text"])]
        groups: dict[str, list[dict]] = defaultdict(list)
        for l in links:
            groups[l["id"].split("$")[0]].append(l)
        if not groups:
            return []
        return max(groups.values(), key=len)

    async def extract_grids(self) -> list[dict]:
        grids: list[dict] = []
        for frame in self.page.frames:
            try:
                grids += await frame.evaluate(GRIDS_JS)
            except Exception:
                continue
        return grids

    async def _back_to_subjects(self, probe_id: str) -> None:
        """Subject page -> 'Change Class' -> term page -> same term -> Continue -> subject list."""
        try:
            await self._click(await self._find(self.s.nav_change_class, timeout_ms=8000))
            await self.select_term(same_as=self.term)
            if await self._locate_id(probe_id):
                return
            log.info("Change Class didn't lead back to the subject list")
        except NavigationError as e:
            log.info("Change Class path failed: %s", e)
        log.info("Re-navigating from home")
        await self.page.goto(self.home_url)
        await self._settle()
        await self.open_assignments()
        await self.select_term(same_as=self.term)

    async def scrape(self, userid: str, password: str) -> ScrapeResult:
        try:
            return await self._scrape(userid, password)
        except NavigationError:
            try:
                log.error("Page structure when navigation failed:\n%s", await self.describe_page())
            except Exception:
                pass
            raise

    async def _scrape(self, userid: str, password: str) -> ScrapeResult:
        await self.login(userid, password)
        await self.open_assignments()
        term = self.term = await self.select_term()
        await self._snap("05_subject_list")

        subjects = await self.list_subjects()
        if not subjects:  # maybe the marks are on this page directly
            grids = await self.extract_grids()
            if not grids:
                raise NavigationError("No subjects or grids found on the assignments page")
            return ScrapeResult(term, [SubjectResult("ALL", grids)])

        results: list[SubjectResult] = []
        for i, subj in enumerate(subjects):
            loc = await self._locate_id(subj["id"])
            if loc is None:
                await self._back_to_subjects(subj["id"])
                loc = await self._locate_id(subj["id"])
                if loc is None:
                    raise NavigationError(f"Lost subject link {subj['text']!r}")
            await self._click(loc)
            await self._snap(f"06_subject_{i:02d}")
            results.append(SubjectResult(subj["text"], await self.extract_grids()))
            if i < len(subjects) - 1:
                await self._back_to_subjects(subjects[i + 1]["id"])
        return ScrapeResult(term, results)
