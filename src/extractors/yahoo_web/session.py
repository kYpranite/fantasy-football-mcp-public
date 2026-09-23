"""Persistent Playwright browser session for Yahoo Fantasy.

The user logs in manually in a real browser window; Yahoo handles password,
2FA, and passkeys. The authenticated session lives only in a local browser
profile directory (gitignored) and is never read, copied, or printed by this
code. Nothing here attempts to bypass Yahoo login, CAPTCHA, or 2FA.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROFILE_DIR = PROJECT_ROOT / ".yahoo_browser_profile"
DEFAULT_DEBUG_DIR = PROJECT_ROOT / ".yahoo_browser_debug"

FANTASY_HOST = "football.fantasysports.yahoo.com"
# Hosts Yahoo redirects to when the session is missing or needs consent.
AUTH_HOSTS = ("login.yahoo.com", "guce.yahoo.com", "consent.yahoo.com")

# Query/JSON keys whose values must never be written to disk or printed.
_SENSITIVE_KEY = r"(?:crumb|token|csrf|xsrf|session|auth|cookie|bearer|signature|secret|password|passwd|guid)"
_SENSITIVE_KEY_RE = re.compile(rf"^[\w.-]*{_SENSITIVE_KEY}[\w.-]*$", re.IGNORECASE)
_JSON_PAIR_RE = re.compile(
    rf'("[\w.-]*{_SENSITIVE_KEY}[\w.-]*"\s*:\s*)"(?:[^"\\]|\\.)*"', re.IGNORECASE
)
_QUERY_PAIR_RE = re.compile(rf"([?&;][\w.-]*{_SENSITIVE_KEY}[\w.-]*=)[^&;\"'\s<>]*", re.IGNORECASE)
# Plain and URL-encoded ("%40") addresses, e.g. in Yahoo account-menu login links.
_EMAIL_RE = re.compile(r"[\w.+-]+(?:@|%40)[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)

REDACTED = "REDACTED"


class AuthRequired(RuntimeError):
    """Raised when Yahoo needs a manual login that cannot happen right now."""


def league_url(league_id: str) -> str:
    return f"https://{FANTASY_HOST}/f1/{league_id}"


def is_auth_url(url: str) -> bool:
    """True when the URL is a Yahoo login/consent page rather than fantasy content."""
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in AUTH_HOSTS)


def is_fantasy_url(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == FANTASY_HOST


def redact_url(url: str) -> str:
    """Drop the fragment and replace every query value with a placeholder.

    Query keys are kept (they show which parameters an endpoint uses); values
    are removed because they can carry crumbs or session identifiers.
    """
    parts = urlsplit(url)
    query = urlencode([(k, REDACTED) for k, _ in parse_qsl(parts.query, keep_blank_values=True)])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def scrub_text(text: str) -> str:
    """Best-effort removal of token-like values and email addresses before saving."""
    text = _JSON_PAIR_RE.sub(rf'\1"{REDACTED}"', text)
    text = _QUERY_PAIR_RE.sub(rf"\1{REDACTED}", text)
    return _EMAIL_RE.sub(REDACTED, text)


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.match(key))


def extract_team_links(
    links: Iterable[Tuple[str, str]], league_id: str
) -> List[Tuple[str, str]]:
    """Pick fantasy team links out of (href, text) pairs.

    Yahoo team pages live at ``/f1/<league_id>/<team_id>``. Returns unique
    ``(team_id, team_name)`` pairs sorted by numeric team id; the first
    non-empty link text seen for each team wins.
    """
    pattern = re.compile(rf"/f1/{re.escape(str(league_id))}/(\d+)/?$")
    teams: dict[str, str] = {}
    for href, text in links:
        match = pattern.search(urlsplit(href or "").path)
        name = " ".join((text or "").split())
        if match and name and match.group(1) not in teams:
            teams[match.group(1)] = name
    return sorted(teams.items(), key=lambda item: int(item[0]))


def find_chrome() -> Optional[Path]:
    """Locate an installed Google Chrome executable (CHROME_PATH overrides)."""
    import os
    import shutil

    candidates = [os.environ.get("CHROME_PATH")]
    for base in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(base)
        if root:
            candidates.append(str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    candidates += [shutil.which(name) for name in ("chrome", "google-chrome", "google-chrome-stable")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def manual_login(target_url: str, profile_dir: Path = DEFAULT_PROFILE_DIR) -> None:
    """Open plain Chrome (no automation attached) on the dedicated profile.

    Some identity providers (e.g. "Sign in with Google") refuse sign-in from
    automation-controlled browsers. Logging in through an ordinary Chrome window
    stores the Yahoo session in ``profile_dir``; later automated runs reuse it.
    Blocks until the user closes the window.
    """
    import subprocess

    chrome = find_chrome()
    if chrome is None:
        raise AuthRequired("Google Chrome not found; set CHROME_PATH to chrome.exe.")
    profile_dir.mkdir(parents=True, exist_ok=True)
    print("\nA normal Chrome window is opening with the dedicated Yahoo profile.")
    print("Log into Yahoo there, confirm your league page loads, then CLOSE that window.\n")
    subprocess.run(
        [str(chrome), f"--user-data-dir={profile_dir}", "--no-first-run", "--new-window", target_url],
        check=False,
    )


@contextmanager
def browser_context(
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    headless: bool = False,
    channel: Optional[str] = "chrome",
) -> Iterator["BrowserContext"]:  # noqa: F821 - playwright type, imported lazily
    """Open a persistent browser context backed by ``profile_dir``.

    Tries installed Chrome first; falls back to Playwright's bundled Chromium.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        launch_kwargs = dict(user_data_dir=str(profile_dir), headless=headless, no_viewport=True)
        try:
            context = p.chromium.launch_persistent_context(channel=channel, **launch_kwargs)
        except PlaywrightError:
            if channel is None:
                raise
            print(f"Could not launch '{channel}'; falling back to bundled Chromium.")
            context = p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            yield context
        finally:
            context.close()


def ensure_logged_in(page, target_url: str, interactive: bool = True, timeout_s: int = 300) -> None:
    """Navigate to ``target_url``; if Yahoo asks for login, wait for the user.

    Raises AuthRequired when login is needed but ``interactive`` is False
    (e.g. headless runs) or the user does not finish within ``timeout_s``.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    page.goto(target_url, wait_until="domcontentloaded")
    if is_fantasy_url(page.url) and not is_auth_url(page.url):
        return

    if not interactive:
        raise AuthRequired(
            "Yahoo session expired or missing. Run "
            "'python utils/yahoo_browser_login.py --league-id <id>' (not headless) and log in."
        )

    print("\nYahoo needs you to log in. Complete login in the browser window")
    print(f"(password/2FA/passkey as usual). Waiting up to {timeout_s // 60} minutes...\n")
    try:
        page.wait_for_url(lambda u: is_fantasy_url(u) and not is_auth_url(u), timeout=timeout_s * 1000)
    except PlaywrightTimeout as exc:
        raise AuthRequired("Timed out waiting for Yahoo login.") from exc

    # Yahoo may land on a generic fantasy page after login; go to the target again.
    if urlsplit(page.url).path.rstrip("/") != urlsplit(target_url).path.rstrip("/"):
        page.goto(target_url, wait_until="domcontentloaded")
    if is_auth_url(page.url):
        raise AuthRequired("Still redirected to Yahoo login after sign-in.")
