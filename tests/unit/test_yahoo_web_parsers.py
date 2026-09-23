"""Offline tests for Yahoo web parsers against sanitized fixture HTML.

Fixtures: tests/fixtures/yahoo_web/ (built by build_fixtures.py from a real sync;
league/team/manager names are placeholders). No browser or Yahoo session needed.
"""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.extractors.yahoo_web import parsers  # noqa: E402
from src.extractors.yahoo_web.extractor import validate_snapshot  # noqa: E402
from src.models.league_data import League, LeagueSnapshot, PlayerScan  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "yahoo_web"
LEAGUE_ID = "269337"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def home():
    return parsers.parse_league_home(fixture("home"), LEAGUE_ID)


@pytest.fixture(scope="module")
def settings():
    return parsers.parse_settings(fixture("settings"))


@pytest.fixture(scope="module")
def managers():
    return parsers.parse_managers(fixture("managers"), LEAGUE_ID)


@pytest.fixture(scope="module")
def rosters(home):
    return {
        tid: parsers.parse_team_roster(fixture(f"team_{tid}"), LEAGUE_ID, tid, home.current_week)
        for tid in ("4", "9")
    }


# ------------------------------------------------------------------------ league home


def test_home_league_basics(home):
    assert home.name == "Test League"
    assert home.season == 2026
    assert home.current_week == 3
    assert home.my_team_id == "4"


def test_home_standings(home):
    assert len(home.standings) == 10
    ranks = [s.rank for _, _, s in home.standings]
    assert ranks == list(range(1, 11))
    first_id, first_name, first = home.standings[0]
    assert (first_id, first_name) == ("9", "Team 9")
    assert first.team_key == "nfl.l.269337.t.9"
    assert (first.wins, first.losses, first.ties) == (2, 0, 0)
    assert first.points_for == pytest.approx(361.56)
    assert first.points_against == pytest.approx(226.10)
    assert first.streak == "W-2"
    # Every team has played the same number of games.
    assert {s.wins + s.losses + s.ties for _, _, s in home.standings} == {2}


def test_home_without_standings_raises():
    with pytest.raises(parsers.ParseError):
        parsers.parse_league_home("<html><body>Maintenance</body></html>", LEAGUE_ID)


# --------------------------------------------------------------------------- settings


def test_settings_rules(settings):
    assert settings.raw["League ID#"] == LEAGUE_ID
    assert settings.max_teams == 12  # Yahoo setting; league actually has 10 teams
    assert settings.scoring_type == "Head-to-Head"
    assert settings.roster_positions == [
        "QB", "WR", "WR", "WR", "RB", "RB", "TE", "W/R/T", "W/R/T", "K", "DEF",
        "BN", "BN", "BN", "BN", "IR", "IR",
    ]
    assert settings.uses_faab is True
    assert settings.divisions is False
    assert settings.fractional_points is True
    assert settings.negative_points is True
    assert settings.trade_end_date == "November 28, 2026"
    assert settings.playoffs.num_teams == 6
    assert settings.playoffs.weeks == [15, 16, 17]
    assert settings.playoffs.reseeding is False


def test_settings_scoring(settings):
    rules = {(r.category, r.stat): r for r in settings.scoring}
    assert len(settings.scoring) == 27
    assert rules[("Offense", "Passing Yards")].yards_per_point == 25
    assert rules[("Offense", "Passing Touchdowns")].points == 4
    interceptions = rules[("Offense", "Interceptions")]  # "Yahoo Default" badge stripped
    assert (interceptions.points, interceptions.yahoo_default) == (-2, "-1")
    assert rules[("Offense", "Receptions")].points == 1  # full PPR
    assert rules[("Kickers", "Field Goals Total Yards")].yards_per_point == 10
    assert rules[("Defense/Special Teams", "Points Allowed 35+ points")].points == -4


# --------------------------------------------------------------------------- managers


def test_managers(managers, home):
    by_id = {t.team_id: t for t in managers}
    assert len(managers) == 10
    assert set(by_id) == {tid for tid, _, _ in home.standings}
    commish = by_id["1"]
    assert commish.managers == ["Manager 1"]
    assert commish.is_commissioner is True
    assert commish.faab_remaining == 20
    assert commish.waiver_priority == 2
    assert commish.moves == 5
    assert by_id["4"].moves == 0 and by_id["4"].trades == 0
    assert sorted(t.waiver_priority for t in managers) == list(range(1, 11))


def test_merge_teams_uses_standings_names_and_marks_mine(home, managers):
    teams = parsers.merge_teams(home, managers)
    assert [t.team_id for t in teams] == [tid for tid, _, _ in home.standings]
    mine = [t for t in teams if t.is_mine]
    assert len(mine) == 1 and mine[0].name == "Team 4"
    assert mine[0].faab_remaining == 50


# ----------------------------------------------------------------------------- roster


def test_own_team_roster(rosters, settings):
    roster = rosters["4"]
    assert roster.team_key == "nfl.l.269337.t.4"
    assert len(roster.players) == 14
    qb = roster.players[0]
    assert (qb.name, qb.player_id, qb.player_key) == ("Caleb Williams", "40900", "nfl.p.40900")
    assert (qb.slot, qb.nfl_team, qb.positions) == ("QB", "CHI", ["QB"])
    assert (qb.status, qb.status_full) == ("D", "Doubtful")
    assert qb.bye_week == 10
    assert qb.percent_rostered == 99 and qb.percent_started == 55
    assert qb.is_starter
    defense = roster.players[-1]
    assert (defense.slot, defense.positions, defense.player_id) == ("DEF", ["DEF"], "100017")
    open_, over = parsers.open_slots(settings.roster_positions, roster.players)
    assert open_ == {"BN": 1, "IR": 2} and over == {}


def test_other_team_roster_with_ir(rosters, settings):
    roster = rosters["9"]
    assert len(roster.players) == 17
    ir = [p for p in roster.players if p.slot == "IR"]
    assert {p.status for p in ir} == {"IR-R", "PUP-R"}
    assert all(not p.is_starter for p in ir)
    # Other-team pages use a two-column "Action" header; columns must still line up.
    assert all(p.percent_rostered is not None and p.bye_week is not None for p in roster.players)
    assert all(p.game is None or chr(0xE232) not in p.game for p in roster.players)
    assert parsers.open_slots(settings.roster_positions, roster.players) == ({}, {})


def test_player_keys_unique_and_well_formed(rosters):
    keys = [p.player_key for r in rosters.values() for p in r.players]
    assert len(keys) == len(set(keys))
    assert all(k.startswith("nfl.p.") and k[6:].isdigit() for k in keys)


def test_roster_page_without_tables_raises():
    with pytest.raises(parsers.ParseError):
        parsers.parse_team_roster("<html><body></body></html>", LEAGUE_ID, "4")


# --------------------------------------------------------------------------- matchups


def test_current_week_matchups(home):
    matchups = parsers.parse_matchups(fixture("home"), LEAGUE_ID)
    assert len(matchups) == 5
    assert {m.week for m in matchups} == {home.current_week}
    assert {m.status for m in matchups} == {"Not started yet"}
    keys = [k for m in matchups for k in m.team_keys]
    assert len(keys) == len(set(keys)) == 10
    assert keys == [k for m in matchups for k in m.team_keys]
    mine = next(m for m in matchups if "nfl.l.269337.t.4" in m.team_keys)
    me, opponent = mine.teams
    assert (me.team_key, opponent.team_key) == ("nfl.l.269337.t.4", "nfl.l.269337.t.3")
    assert me.points == 0 and me.projected_points == pytest.approx(119.98)
    assert opponent.projected_points == pytest.approx(145.75)


def test_matchups_missing_module_raises():
    with pytest.raises(parsers.ParseError):
        parsers.parse_matchups("<html><body></body></html>", LEAGUE_ID)


# ------------------------------------------------------------------------ player list


def test_player_list_first_page_has_next():
    page = parsers.parse_player_list(fixture("players_O"))
    assert len(page.rows) == 25
    assert page.has_next is True
    player, value = page.rows[0]
    assert (player.name, player.player_key, player.positions) == ("Jared Goff", "nfl.p.29235", ["QB"])
    assert (player.availability, player.waiver_until) == ("waivers", "Sep 23")
    assert player.nfl_team == "DET" and player.bye_week == 6 and player.games_played == 15
    assert player.percent_rostered == 84 and player.preseason_rank == 104
    assert value == pytest.approx(259.33)  # "Fan Pts" for this view (rest-of-season projection)
    values = [v for _, v in page.rows]
    assert values == sorted(values, reverse=True)  # requested sort=PTS descending
    assert len({p.player_id for p, _ in page.rows}) == 25


def test_player_list_last_page_has_no_next():
    page = parsers.parse_player_list(fixture("players_DEF"))
    assert page.has_next is False
    assert 0 < len(page.rows) < 25
    assert all(p.positions == ["DEF"] and int(p.player_id) > 100000 for p, _ in page.rows)


@pytest.mark.parametrize(
    "label, expected",
    [("FA", ("free_agent", None)), ("W (Sep 23)", ("waivers", "Sep 23")), ("W", ("waivers", None))],
)
def test_availability_labels(label, expected):
    assert parsers._availability(label) == expected


def test_available_players_not_on_fixture_rosters(rosters):
    rostered = {p.player_id for r in rosters.values() for p in r.players}
    listed = {p.player_id for name in ("players_O", "players_DEF") for p, _ in parsers.parse_player_list(fixture(name)).rows}
    assert rostered.isdisjoint(listed)


# ------------------------------------------------------------------------- validation


def _snapshot(home, settings, managers, rosters, team_ids=("4", "9")):
    teams = [t for t in parsers.merge_teams(home, managers) if t.team_id in team_ids]
    standings = [s for tid, _, s in home.standings if tid in team_ids]
    return LeagueSnapshot(
        captured_at="2026-09-22T00:00:00+00:00",
        source="yahoo_web",
        league=League(
            league_key="nfl.l.269337", league_id=LEAGUE_ID, name="Test League", season=2026,
            current_week=3, num_teams=len(teams), url="https://example.invalid",
        ),
        settings=settings,
        teams=teams,
        standings=standings,
        rosters=[rosters[tid] for tid in team_ids if tid in rosters],
    )


def test_validation_passes_for_consistent_snapshot(home, settings, managers, rosters):
    errors, warnings = validate_snapshot(_snapshot(home, settings, managers, rosters))
    assert errors == []
    assert any("Max Teams" in w for w in warnings)


def test_validation_flags_missing_roster(home, settings, managers, rosters):
    snapshot = _snapshot(home, settings, managers, rosters, team_ids=("4", "9", "1"))
    errors, _ = validate_snapshot(snapshot)
    assert any("rosters missing" in e and "t.1" in e for e in errors)


def test_validation_flags_player_on_two_teams(home, settings, managers, rosters):
    snapshot = _snapshot(home, settings, managers, rosters)
    stolen = replace(rosters["4"].players[0], team_key=rosters["9"].team_key)
    snapshot.rosters[1] = replace(rosters["9"], players=rosters["9"].players + [stolen])
    errors, _ = validate_snapshot(snapshot)
    assert any("on two rosters" in e for e in errors)


def test_validation_flags_missing_week_and_owner(home, settings, managers, rosters):
    snapshot = _snapshot(home, settings, managers, rosters)
    snapshot.league.current_week = None
    for team in snapshot.teams:
        team.is_mine = False
    errors, _ = validate_snapshot(snapshot)
    assert any("current week" in e for e in errors)
    assert any("exactly one team" in e for e in errors)


def test_validation_flags_bad_matchups(home, settings, managers, rosters):
    snapshot = _snapshot(home, settings, managers, rosters)
    matchups = parsers.parse_matchups(fixture("home"), LEAGUE_ID)
    snapshot.matchups = [m for m in matchups if set(m.team_keys) & {t.team_key for t in snapshot.teams}]
    errors, _ = validate_snapshot(snapshot)
    assert any("unknown teams" in e for e in errors)  # opponents 3 and 1 are not in this 2-team snapshot
    snapshot.matchups = matchups[:1] + matchups[:1]
    errors, _ = validate_snapshot(snapshot)
    assert any("more than one current-week matchup" in e for e in errors)


def test_validation_flags_rostered_player_listed_available_and_depth_cap(home, settings, managers, rosters):
    snapshot = _snapshot(home, settings, managers, rosters)
    available = [p for p, _ in parsers.parse_player_list(fixture("players_O")).rows]
    rostered = rosters["4"].players[0]
    snapshot.available_players = available + [replace(available[0], player_key=rostered.player_key, name=rostered.name)]
    snapshot.player_scans = [PlayerScan("O", "S_PSR_2026", pages=4, players=100, reached_end=False)]
    errors, warnings = validate_snapshot(snapshot)
    assert errors == []
    assert any("both rostered and available" in w and rostered.name in w for w in warnings)
    assert any("capped per view" in w and "O top 100" in w for w in warnings)
