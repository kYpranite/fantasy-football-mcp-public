#!/usr/bin/env python3
"""Build sanitized, trimmed Yahoo web fixtures from pages saved by a sync run.

    python utils/sync_yahoo_league.py --league-id <id> --save-pages
    python tests/fixtures/yahoo_web/build_fixtures.py <saved pages dir> --teams 4 9

Keeps only the elements the parsers read, and replaces league name, team names,
manager names, custom league URL slug, and profile links with placeholders.
Review the output before committing (git add -f; tests/ is gitignored).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from src.extractors.yahoo_web.parsers import parse_managers, parse_settings  # noqa: E402
from src.extractors.yahoo_web.session import scrub_text  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent
FIXTURE_LEAGUE_NAME = "Test League"

KEEP = {
    "home": ["#seasonspec", "#matchupweek", "#standingstable"],
    "settings": ["#settings-table", "#settings-stat-mod-table"],
    "managers": ["table"],
    "team": ["#statTable0", "#statTable1", "#statTable2"],
    "players": ["table.Table-interactive", ".pagingnavlist"],
    "transactions": [".Tst-transaction-table", ".pagingnavlist"],
    "draft": ["#yspmaincontent table"],
    "week": ["#matchupweek"],
}
# fixture name → saved page name for history pages
HISTORY_PAGES = {
    "transactions_p1": "transactions_@0",
    "transactions_p2": "transactions_@25",
    "transactions_faab": "FAB_offers_@0",
    "draft": "draft_results",
    "week_1": "week_1_matchups",
}
# fixture name → saved page name (from sync --save-pages) for available-player lists
PLAYER_PAGES = {
    "players_O": "players_O_S_PSR_{season}_@0",
    "players_DEF": "players_DEF_S_PSR_{season}_@0",
}


def trim(html: str, selectors: list[str]) -> str:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text() if soup.title else ""
    parts = []
    for selector in selectors:
        for el in soup.select(selector):
            for junk in el.select("script, style, svg, img, iframe"):
                junk.decompose()
            parts.append(str(el))
    return f"<html><head><title>{title}</title></head><body>\n" + "\n".join(parts) + "\n</body></html>\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pages_dir", type=Path)
    parser.add_argument("--teams", nargs="+", default=["4", "9"])
    parser.add_argument("--league-id", default="269337")
    parser.add_argument("--season", default="2026")
    args = parser.parse_args()

    read = lambda name: (args.pages_dir / f"{name}.html").read_text(encoding="utf-8")  # noqa: E731
    settings = parse_settings(read("settings"))
    managers = parse_managers(read("managers"), args.league_id)

    replacements: list[tuple[str, str]] = []
    for team in managers:
        replacements.append((team.name, f"Team {team.team_id}"))
        for manager in team.managers:
            replacements.append((manager, f"Manager {team.team_id}"))
    league_name = settings.raw.get("League Name", "")
    if league_name:
        replacements.append((league_name, FIXTURE_LEAGUE_NAME))
    slug = settings.raw.get("Custom League URL", "").rstrip("/").rsplit("/", 1)[-1]
    if slug and "." not in slug:
        replacements.append((slug, "example_league"))
    # Longest first so "Ryan Smith" is replaced before "Ryan".
    replacements.sort(key=lambda pair: len(pair[0]), reverse=True)

    def sanitize(html: str) -> str:
        html = scrub_text(html)
        html = re.sub(r'https://profiles\.sports\.yahoo\.com/user/[A-Za-z0-9]+', "#", html)
        for real, fake in replacements:
            html = html.replace(real, fake)
            html = html.replace(real.replace("'", "&#39;"), fake)
            html = html.replace(real.replace("'", "&apos;"), fake)
        return html

    sources = {"home": "home", "settings": "settings", "managers": "managers"}
    sources.update({f"team_{t}": f"team_{t}_roster" for t in args.teams})
    sources.update({name: page.format(season=args.season) for name, page in PLAYER_PAGES.items()})
    sources.update(HISTORY_PAGES)
    for fixture, source in sources.items():
        prefixed = ("team_", "players_", "transactions_", "week_")
        kind = fixture.split("_")[0] if fixture.startswith(prefixed) else fixture
        html = sanitize(trim(read(source), KEEP[kind]))
        leftovers = [real for real, _ in replacements if real in html]
        if leftovers:
            print(f"{fixture}: names not replaced: {leftovers}")
            return 1
        (OUT_DIR / f"{fixture}.html").write_text(html, encoding="utf-8")
        print(f"wrote {fixture}.html ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
