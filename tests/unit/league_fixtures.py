"""Shared test helpers: build realistic LeagueSnapshots from sanitized Yahoo web fixtures."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.extractors.yahoo_web import parsers  # noqa: E402
from src.models.league_data import League, LeagueSnapshot, PlayerScan  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "yahoo_web"
LEAGUE_ID = "269337"
LEAGUE_KEY = "nfl.l.269337"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def build_snapshot(captured_at="2026-09-22T21:00:00+00:00") -> LeagueSnapshot:
    """A realistic snapshot from fixtures: teams 4 and 9 with rosters, plus all history."""
    home = parsers.parse_league_home(fixture("home"), LEAGUE_ID)
    settings = parsers.parse_settings(fixture("settings"))
    managers = parsers.parse_managers(fixture("managers"), LEAGUE_ID)
    team_ids = ("4", "9")
    teams = [t for t in parsers.merge_teams(home, managers) if t.team_id in team_ids]
    names = {t.name: t.team_key for t in parsers.merge_teams(home, managers)}
    rosters = []
    for tid in [t.team_id for t in teams]:  # same order as the extractor: standings order
        roster = parsers.parse_team_roster(fixture(f"team_{tid}"), LEAGUE_ID, tid, home.current_week)
        roster.empty_slots, _ = parsers.open_slots(settings.roster_positions, roster.players)
        rosters.append(roster)
    available = []
    for name in ("players_O", "players_DEF"):  # both fixtures are the rest-of-season projection view
        for player, value in parsers.parse_player_list(fixture(name)).rows:
            player.projected_rest_of_season = value
            available.append(player)
    transactions = (
        parsers.parse_transactions(fixture("transactions_p1"), LEAGUE_ID, 2026)[0]
        + parsers.parse_transactions(fixture("transactions_p2"), LEAGUE_ID, 2026)[0]
    )
    return LeagueSnapshot(
        captured_at=captured_at,
        source="yahoo_web",
        league=League(LEAGUE_KEY, LEAGUE_ID, "Test League", home.season, home.current_week, len(teams),
                      "https://football.fantasysports.yahoo.com/f1/269337"),
        settings=settings,
        teams=teams,
        standings=[s for tid, _, s in home.standings if tid in team_ids],
        rosters=rosters,
        matchups=parsers.parse_matchups(fixture("home"), LEAGUE_ID),
        available_players=available,
        player_scans=[PlayerScan("O", "S_PSR_2026", 1, 25, False)],
        matchup_history=parsers.parse_matchups(fixture("week_1"), LEAGUE_ID),
        transactions=transactions,
        waiver_claims=parsers.parse_waiver_claims(fixture("transactions_faab"), LEAGUE_ID, 2026)[0],
        draft_picks=parsers.parse_draft_results(fixture("draft"), names),
        warnings=["example warning"],
    )
