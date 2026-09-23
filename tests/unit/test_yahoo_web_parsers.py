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


# ---------------------------------------------------------------------------- history


@pytest.fixture(scope="module")
def names(home):
    return {name: f"nfl.l.269337.t.{tid}" for tid, name, _ in home.standings}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Sep 22, 6:04 pm", "2026-09-22T18:04:00"),
        ("Sep 16,4:44 am", "2026-09-16T04:44:00"),  # FAB page omits the space
        ("Dec 1, 12:05 am", "2026-12-01T00:05:00"),
        ("Jan 3, 12:30 pm", "2027-01-03T12:30:00"),  # playoffs roll into next year
        ("yesterday", None),
    ],
)
def test_parse_timestamp(raw, expected):
    assert parsers.parse_timestamp(raw, 2026) == expected


def test_transactions_first_page(names):
    txs, has_next = parsers.parse_transactions(fixture("transactions_p1"), LEAGUE_ID, 2026)
    assert has_next is True and len(txs) == 25
    assert all(t.team_key in names.values() for t in txs)
    assert len({t.transaction_id for t in txs}) == 25
    latest = txs[0]
    assert latest.type == "drop" and latest.timestamp == "2026-09-22T18:04:00"
    (dropped,) = latest.players
    assert (dropped.name, dropped.player_key, dropped.action, dropped.detail) == (
        "Michael Mayer", "nfl.p.40065", "drop", "To Waivers")
    claim = next(t for t in txs if any(p.faab_bid is not None for p in t.players))
    add, drop = claim.players
    assert claim.type == "add/drop"
    assert (add.action, add.detail, add.faab_bid) == ("add", "$0 Waiver", 0)
    assert drop.action == "drop" and drop.faab_bid is None


def test_transactions_last_page_and_defenses():
    txs, has_next = parsers.parse_transactions(fixture("transactions_p2"), LEAGUE_ID, 2026)
    assert has_next is False and 0 < len(txs) < 25
    txs += parsers.parse_transactions(fixture("transactions_p1"), LEAGUE_ID, 2026)[0]
    defenses = [p for t in txs for p in t.players if p.positions == ["DEF"]]
    assert defenses and all(int(p.player_id) > 100000 for p in defenses)
    # Yahoo labels the same DEF "Chiefs" or "Kansas City" between loads: join on ids, not names.
    chiefs = next(p for p in defenses if p.player_key == "nfl.p.100012")
    assert chiefs.nfl_team == "KC" and chiefs.name in ("Chiefs", "Kansas City")


def test_waiver_claims_include_losing_bids(names):
    claims, has_next = parsers.parse_waiver_claims(fixture("transactions_faab"), LEAGUE_ID, 2026)
    assert has_next is False and len(claims) == 4
    vele = next(c for c in claims if c.name == "Devaughn Vele")
    assert vele.winning_bid == 18 and vele.awarded_team_key == names["Team 5"]
    assert [(b.bid, b.result) for b in vele.bids] == [(18, "won"), (8, "Lower Offer"), (7, "Lower Offer")]
    assert vele.bids[1].team_key == names["Team 1"]
    niners = next(c for c in claims if c.name == "49ers")
    assert niners.player_key == "nfl.p.100025"  # DEF id from data-ys-playerid
    assert niners.bids[1].result == "Lower waiver priority"


def test_draft_results(names):
    picks = parsers.parse_draft_results(fixture("draft"), names)
    assert len(picks) == 150
    assert [p.overall for p in picks] == list(range(1, 151))
    assert all(p.team_key is not None for p in picks)
    first = picks[0]
    assert (first.round, first.pick, first.name, first.player_key) == (1, 1, "Jahmyr Gibbs", "nfl.p.40059")
    assert (first.nfl_team, first.position, first.team_key) == ("DET", "RB", names["Team 11"])
    # Each team drafts once per round.
    for round_no in range(1, 16):
        assert len({p.team_key for p in picks if p.round == round_no}) == 10
    defenses = {p.name: p.player_id for p in picks if p.position == "DEF"}
    assert len(defenses) == 10 and defenses["Patriots"] == "100017"  # mapped from team, no id in markup
    assert len({p.player_key for p in picks}) == 150


def test_draft_unknown_team_name_left_unmatched(names):
    renamed = {k: v for k, v in names.items() if k != "Team 11"}
    picks = parsers.parse_draft_results(fixture("draft"), renamed)
    assert {p.team_name for p in picks if p.team_key is None} == {"Team 11"}


def test_past_week_matchups_final_with_winners():
    matchups = parsers.parse_matchups(fixture("week_1"), LEAGUE_ID)
    assert len(matchups) == 5 and {m.week for m in matchups} == {1}
    assert all("final" in m.status.lower() for m in matchups)
    mine = next(m for m in matchups if "nfl.l.269337.t.4" in m.team_keys)
    assert mine.teams[0].points == pytest.approx(142.86)
    assert mine.winner_team_key == "nfl.l.269337.t.4"
    assert all(m.winner_team_key in m.team_keys for m in matchups)


def test_unfinished_matchup_has_no_winner():
    current = parsers.parse_matchups(fixture("home"), LEAGUE_ID)
    assert all(m.winner_team_key is None for m in current)


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


def test_validation_history_problems_are_warnings(home, settings, managers, rosters, names):
    snapshot = _snapshot(home, settings, managers, rosters)
    snapshot.draft_picks = parsers.parse_draft_results(fixture("draft"), names)[:7]
    snapshot.draft_picks[0] = replace(snapshot.draft_picks[0], team_key=None, team_name="Old Name")
    errors, warnings = validate_snapshot(snapshot)
    assert errors == []
    assert any("Old Name" in w for w in warnings)
    assert any("draft has 7 picks" in w for w in warnings)
