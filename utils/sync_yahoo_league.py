#!/usr/bin/env python3
"""Sync league data from the Yahoo Fantasy website using the saved browser session.

Extracts league metadata, settings/scoring, teams/managers (FAAB, waiver priority),
standings, every team's current roster, current-week matchups, and available players
(free agents + waivers, paged). Nothing is saved unless the whole extraction passes
validation.

Storage is not decided yet: for now a validated snapshot is written as JSON to
.yahoo_browser_debug/snapshots/ (gitignored) for inspection.

Usage (PowerShell):
    python utils/sync_yahoo_league.py --league-id 269337
    python utils/sync_yahoo_league.py --league-id 269337 --save-pages   # also keep scrubbed HTML
    python utils/sync_yahoo_league.py --league-id 269337 --offense-depth 200
    python utils/sync_yahoo_league.py --league-id 269337 --no-players   # skip available players
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.extractors.yahoo_web.extractor import (  # noqa: E402
    DEFAULT_PLAYER_DEPTH,
    ExtractionError,
    YahooWebExtractor,
)
from src.extractors.yahoo_web.session import (  # noqa: E402
    DEFAULT_DEBUG_DIR,
    AuthRequired,
    browser_context,
    ensure_logged_in,
    league_url,
    scrub_text,
)


def print_summary(snapshot) -> None:
    league = snapshot.league
    print(f"\n{league.name} ({league.league_key}) — season {league.season}, week {league.current_week}")
    print(f"{league.num_teams} teams, roster slots: {', '.join(snapshot.settings.roster_positions)}")
    standings = {s.team_key: s for s in snapshot.standings}
    rosters = {r.team_key: r for r in snapshot.rosters}
    for team in sorted(snapshot.teams, key=lambda t: standings[t.team_key].rank or 99):
        s = standings[team.team_key]
        mine = " (you)" if team.is_mine else ""
        faab = f"${team.faab_remaining:g}" if team.faab_remaining is not None else "-"
        print(
            f"  {s.rank:>2}. {team.name}{mine} — {s.wins}-{s.losses}-{s.ties}, "
            f"PF {s.points_for}, FAAB {faab}, {len(rosters[team.team_key].players)} players"
        )

    names = {t.team_key: t.name for t in snapshot.teams}
    if snapshot.matchups:
        print(f"\nWeek {snapshot.matchups[0].week} matchups ({snapshot.matchups[0].status}):")
        for m in snapshot.matchups:
            a, b = m.teams
            print(
                f"  {names[a.team_key]} {a.points} (proj {a.projected_points})"
                f"  vs  {names[b.team_key]} {b.points} (proj {b.projected_points})"
            )

    if snapshot.available_players:
        counts = {}
        for p in snapshot.available_players:
            counts[p.availability] = counts.get(p.availability, 0) + 1
        print(f"\nAvailable players: {len(snapshot.available_players)} {counts}")
        for position in ("QB", "RB", "WR", "TE", "K", "DEF"):
            top = [p for p in snapshot.available_players if position in p.positions][:3]
            if top:
                listed = ", ".join(f"{p.name} ({p.projected_rest_of_season} ROS)" for p in top)
                print(f"  {position}: {listed}")

    for warning in snapshot.warnings:
        print(f"  warning: {warning}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league-id", default=os.environ.get("YAHOO_LEAGUE_ID"), help="Number from /f1/<id> (or YAHOO_LEAGUE_ID)")
    parser.add_argument("--headless", action="store_true", help="No browser window (fails if login is needed)")
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds between page loads (default 2)")
    parser.add_argument("--save-pages", action="store_true", help="Keep scrubbed HTML of every page fetched")
    parser.add_argument("--no-players", action="store_true", help="Skip available players (free agents/waivers)")
    parser.add_argument(
        "--offense-depth",
        type=int,
        default=DEFAULT_PLAYER_DEPTH["O"],
        help=f"Available offensive players to collect per view (default {DEFAULT_PLAYER_DEPTH['O']})",
    )
    args = parser.parse_args()
    if not args.league_id:
        parser.error("--league-id is required (or set YAHOO_LEAGUE_ID)")

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pages_dir = DEFAULT_DEBUG_DIR / "pages" / f"league_{args.league_id}_{stamp}"

    def save_page(name: str, html: str) -> None:
        pages_dir.mkdir(parents=True, exist_ok=True)
        (pages_dir / f"{name.replace(' ', '_')}.html").write_text(scrub_text(html), encoding="utf-8")

    print(f"Syncing league {args.league_id} from Yahoo web...")
    try:
        with browser_context(headless=args.headless) as context:
            page = context.pages[0] if context.pages else context.new_page()
            ensure_logged_in(page, league_url(args.league_id), interactive=not args.headless)
            extractor = YahooWebExtractor(
                page, args.league_id, delay_s=args.delay, on_page=save_page if args.save_pages else None
            )
            depth = None if args.no_players else {**DEFAULT_PLAYER_DEPTH, "O": args.offense_depth}
            snapshot = extractor.extract(player_depth=depth)
    except AuthRequired as exc:
        print(f"\nLogin required: {exc}")
        return 2
    except ExtractionError as exc:
        print(f"\nSync FAILED — nothing saved.\n{exc}")
        return 1

    print_summary(snapshot)
    out = DEFAULT_DEBUG_DIR / "snapshots" / f"league_{args.league_id}_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSnapshot (temporary JSON, storage TBD): {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
