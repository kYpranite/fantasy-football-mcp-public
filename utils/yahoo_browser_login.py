#!/usr/bin/env python3
"""Proof of concept: log into Yahoo Fantasy in a persistent browser and read a league.

Opens a real browser window backed by a local profile (.yahoo_browser_profile/).
If Yahoo asks for login, sign in manually; the session is reused on later runs.
Prints the league name and team names, and writes a sanitized discovery report
(JSON endpoints seen, embedded script state, page HTML) to .yahoo_browser_debug/
so the extraction strategy can be chosen from what Yahoo actually serves.

Usage (PowerShell):
    python utils/yahoo_browser_login.py --league-id 269337
    python utils/yahoo_browser_login.py --league-id 269337 --manual-login   # Google sign-in
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.extractors.yahoo_web.session import (  # noqa: E402
    DEFAULT_DEBUG_DIR,
    DEFAULT_PROFILE_DIR,
    REDACTED,
    AuthRequired,
    browser_context,
    ensure_logged_in,
    extract_team_links,
    is_sensitive_key,
    league_url,
    manual_login,
    redact_url,
    scrub_text,
)

MAX_SAVED_BODY_BYTES = 2_000_000

SCRIPT_SUMMARY_JS = r"""
() => Array.from(document.scripts).map((s, i) => {
  const text = s.textContent || "";
  const assigns = Array.from(new Set(
    (text.match(/(?:window|self|root(?:\.App)?)\.[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*=/g) || [])
      .map(m => m.replace(/\s*=$/, ""))
  )).slice(0, 20);
  return { index: i, id: s.id || null, type: s.type || null, src: s.src || null,
           length: text.length, assigns, text: s.type && s.type.includes("json") ? text : null };
})
"""


def summarize_shape(value: Any, depth: int = 3) -> Any:
    """Structure of a JSON value without its data (keys and types only)."""
    if isinstance(value, dict):
        if depth == 0:
            return f"object({len(value)} keys)"
        return {
            k: (REDACTED if is_sensitive_key(k) else summarize_shape(v, depth - 1))
            for k, v in list(value.items())[:40]
        }
    if isinstance(value, list):
        if not value or depth == 0:
            return f"array({len(value)})"
        return [f"array({len(value)})", summarize_shape(value[0], depth - 1)]
    return type(value).__name__


def is_fantasy_related(url: str) -> bool:
    """Yahoo sports/fantasy traffic worth saving; skips ad, pixel, and telemetry hosts."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host.endswith("yahoo.com") or host.startswith(("pbs.", "ads.", "geo.", "udc.")):
        return False
    return "fantasysports" in host or host == "sports.yahoo.com" or "fantasy" in parts.path.lower()


def attach_response_recorder(page) -> List[Any]:
    responses: List[Any] = []

    def on_response(response) -> None:
        resource_type = response.request.resource_type
        content_type = response.headers.get("content-type", "")
        if resource_type in ("xhr", "fetch") or "json" in content_type:
            responses.append(response)

    page.on("response", on_response)
    return responses


def describe_responses(responses: List[Any], out_dir: Path) -> List[Dict[str, Any]]:
    entries = []
    body_dir = out_dir / "responses"
    for n, response in enumerate(responses):
        entry: Dict[str, Any] = {
            "n": n,
            "method": response.request.method,
            "url": redact_url(response.url),
            "status": response.status,
            "resource_type": response.request.resource_type,
            "content_type": response.headers.get("content-type", ""),
        }
        if "json" in entry["content_type"] and is_fantasy_related(response.url):
            try:
                body = response.body()
                entry["bytes"] = len(body)
                entry["shape"] = summarize_shape(json.loads(body))
                if len(body) <= MAX_SAVED_BODY_BYTES:
                    body_dir.mkdir(exist_ok=True)
                    (body_dir / f"{n:03d}.json").write_text(
                        scrub_text(body.decode("utf-8", errors="replace")), encoding="utf-8"
                    )
            except Exception as exc:  # body evicted, not JSON, etc.
                entry["body_error"] = type(exc).__name__
        entries.append(entry)
    return entries


def describe_scripts(scripts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    described = []
    for script in scripts:
        text = script.pop("text")
        if script["src"]:
            script["src"] = redact_url(script["src"])
        if text:
            try:
                script["json_shape"] = summarize_shape(json.loads(text))
            except ValueError:
                script["json_shape"] = "unparseable"
        if script["length"] or script["src"]:
            described.append(script)
    return described


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league-id", required=True, help="Number from /f1/<id> in the league URL")
    parser.add_argument("--headless", action="store_true", help="No window; fails if login is needed")
    parser.add_argument("--channel", default="chrome", help="Browser channel ('chrome', or 'none' for bundled Chromium)")
    parser.add_argument("--login-timeout", type=int, default=300, help="Seconds to wait for manual login")
    parser.add_argument(
        "--manual-login",
        action="store_true",
        help="Log in first through a normal (non-automated) Chrome window; use if Yahoo/Google blocks sign-in",
    )
    parser.add_argument("--no-report", action="store_true", help="Skip writing the discovery report")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    channel = None if args.channel.lower() == "none" else args.channel
    target = league_url(args.league_id)
    out_dir = DEFAULT_DEBUG_DIR / f"league_{args.league_id}_{datetime.now():%Y%m%d_%H%M%S}"

    print(f"Browser profile: {DEFAULT_PROFILE_DIR}")
    try:
        if args.manual_login:
            manual_login(target)
        with browser_context(headless=args.headless, channel=channel) as context:
            page = context.pages[0] if context.pages else context.new_page()
            ensure_logged_in(page, target, interactive=not args.headless, timeout_s=args.login_timeout)

            # Record traffic on a clean load of the league page only.
            responses = attach_response_recorder(page)
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass  # ads/analytics can keep the network busy; DOM is ready regardless

            title_candidates = page.evaluate(
                """() => ({
                    document_title: document.title,
                    og_title: document.querySelector('meta[property="og:title"]')?.content || null,
                    h1: Array.from(document.querySelectorAll('h1')).map(e => e.innerText.trim()).filter(Boolean).slice(0, 5),
                })"""
            )
            links = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.href, e.innerText])")
            teams = extract_team_links(links, args.league_id)

            print(f"\nPage: {redact_url(page.url)}")
            print(f"Title: {title_candidates['document_title']}")
            if title_candidates["h1"]:
                print(f"Headings: {' | '.join(title_candidates['h1'])}")
            print(f"\nTeams found: {len(teams)}")
            for team_id, name in teams:
                print(f"  {team_id:>3}  {name}")

            if not args.no_report:
                out_dir.mkdir(parents=True, exist_ok=True)
                # Page-bound data first, then response bodies (slowest step), so a
                # closed window still leaves a usable report.
                report = {
                    "league_id": args.league_id,
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                    "page_url": redact_url(page.url),
                    "title_candidates": title_candidates,
                    "teams": [{"team_id": t, "name": n} for t, n in teams],
                    "scripts": describe_scripts(page.evaluate(SCRIPT_SUMMARY_JS)),
                }
                (out_dir / "page.html").write_text(scrub_text(page.content()), encoding="utf-8")
                try:
                    report["network"] = describe_responses(responses, out_dir)
                finally:
                    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(f"\nDiscovery report: {out_dir}")
    except AuthRequired as exc:
        print(f"\nLogin required: {exc}")
        return 2

    if len(teams) < 2:
        print("\nCould not identify team names from the league page; check the discovery report.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
