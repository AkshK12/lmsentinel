"""Settings loaded from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    base_url: str = field(default_factory=lambda: _env("LMS_BASE_URL", "https://campus.thapar.edu").rstrip("/"))
    site: str = field(default_factory=lambda: _env("LMS_SITE", "csprd"))
    headless: bool = field(default_factory=lambda: _env("HEADLESS", "true").lower() != "false")
    poll_interval: int = field(default_factory=lambda: int(_env("POLL_INTERVAL_SECONDS", "900")))
    poll_jitter: int = field(default_factory=lambda: int(_env("POLL_JITTER_SECONDS", "120")))
    browser_channel: str = field(default_factory=lambda: _env("BROWSER_CHANNEL", ""))
    timeout_ms: int =field(default_factory=lambda: int(_env("NAV_TIMEOUT_MS", "45000")))
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", str(ROOT / "data" / "grades.db"))))

    # Navigation (regexes, case-insensitive). Tweak these after running `recon`.
    nav_grades: str = field(default_factory=lambda: _env("NAV_GRADES_REGEX", r"view\s*my\s*grades"))
    nav_assignments: str = field(default_factory=lambda: _env("NAV_ASSIGNMENTS_REGEX", r"view\s*my\s*assignments"))
    nav_continue: str = field(default_factory=lambda: _env("NAV_CONTINUE_REGEX", r"^\s*continue\s*$"))
    nav_return: str = field(default_factory=lambda: _env("NAV_RETURN_REGEX", r"^\s*(return|back)\b"))
    nav_change_class: str = field(default_factory=lambda: _env("NAV_CHANGE_CLASS_REGEX", r"change\s*class"))
    # Optional: direct PeopleSoft component URL of the "View My Assignments" page (skips the 2 clicks).
    assignments_url: str = field(default_factory=lambda: _env("ASSIGNMENTS_URL", ""))
    # "last" (Thapar lists oldest first, so last = latest) | "first" | regex matched against the term row
    term_strategy: str = field(default_factory=lambda: _env("TERM_STRATEGY", "last"))
    # Optional regex to identify subject links on the subject-list page
    subject_link_regex: str = field(default_factory=lambda: _env("SUBJECT_LINK_REGEX", ""))

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/psp/{self.site}/?cmd=login&languageCd=ENG"

    @property
    def logout_url(self) -> str:
        return f"{self.base_url}/psp/{self.site}/?cmd=logout"


KEYRING_SERVICE = "thapar-lms-scraper"
ENV_FILE = ROOT / ".env"


def get_account() -> tuple[str, str] | None:
    """This laptop's account: user ID from .env, password from the OS keychain
    (Windows Credential Manager / macOS Keychain / Secret Service), or LMS_PASSWORD in .env as fallback."""
    uid = os.getenv("LMS_USERID", "").strip().strip("'\"")
    if not uid:
        return None
    pwd = os.getenv("LMS_PASSWORD", "")
    if not pwd:
        try:
            import keyring
            pwd = keyring.get_password(KEYRING_SERVICE, uid) or ""
        except Exception:
            pwd = ""
    return (uid, pwd) if pwd else None
