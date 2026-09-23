"""Tests for the local data source and local MCP handlers (no Yahoo, no network).

A temporary SQLite store is filled from sanitized fixtures (teams 4 and 9 of the
fixture league, plus history), then queried the way MCP tools do.
"""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from league_fixtures import LEAGUE_KEY, build_snapshot  # noqa: E402
from src.datasource.local_source import (  # noqa: E402
    LocalLeagueSource,
    NoLocalData,
    normalize_league_key,
    normalize_team_key,
)
from src.storage.db import connect  # noqa: E402
from src.storage.repository import LeagueStore  # noqa: E402

ME, OTHER = "nfl.l.269337.t.4", "nfl.l.269337.t.9"


@pytest.fixture
def source(tmp_path):
    db = tmp_path / "league.db"
    store = LeagueStore(connect(db))
    store.save_snapshot(build_snapshot())
    yield LocalLeagueSource(db_path=db, store=store)
    store.conn.close()


# ------------------------------------------------------------------------- keys


@pytest.mark.parametrize("raw", ["nfl.l.269337", "461.l.269337", "269337", "461.l.269337.t.4"])
def test_league_key_normalization(raw):
    assert normalize_league_key(raw) == LEAGUE_KEY


@pytest.mark.parametrize("raw", ["4", "nfl.l.269337.t.4", "461.l.269337.t.4"])
def test_team_key_normalization(raw):
    assert normalize_team_key(raw, LEAGUE_KEY) == ME


def test_league_key_optional_with_single_league(source):
    assert source.resolve_league(None) == LEAGUE_KEY
    assert source.league_info("461.l.269337")["league"] == "Test League"


# ---------------------------------------------------------------- league data


def test_leagues_and_info(source):
    leagues = source.leagues()
    assert leagues["total_leagues"] == 1 and leagues["leagues"][0]["key"] == LEAGUE_KEY
    info = source.league_info(LEAGUE_KEY)
    assert info["current_week"] == 3 and info["your_team"]["key"] == ME
    assert info["waivers"]["uses_faab"] is True
    assert info["synced_at"] and info["data_source"] == "local"


def test_settings_include_scoring_and_raw(source):
    settings = source.league_settings(LEAGUE_KEY)
    assert len(settings["scoring"]) == 27
    assert settings["roster_positions"].count("BN") == 4
    assert settings["all_settings"]["Waiver Time"] == "2 days"


def test_standings_sorted_with_mine_flag(source):
    rows = source.standings(LEAGUE_KEY)["standings"]
    assert [r["rank"] for r in rows] == sorted(r["rank"] for r in rows)
    assert [r["team_key"] for r in rows if r["is_mine"]] == [ME]
    assert all(r["faab_remaining"] is not None for r in rows)


def test_roster_defaults_to_my_team_and_splits_slots(source):
    mine = source.roster(LEAGUE_KEY)
    assert mine["team_key"] == ME and mine["is_mine"] is True
    assert len(mine["starters"]) + len(mine["bench"]) + len(mine["injured_reserve"]) == len(mine["roster"])
    assert mine["open_slots"] == {"BN": 1, "IR": 2}
    other = source.roster(LEAGUE_KEY, "9")
    assert other["team_key"] == OTHER and len(other["injured_reserve"]) == 2


def test_unknown_team_raises(source):
    with pytest.raises(NoLocalData, match="Unknown team"):
        source.roster(LEAGUE_KEY, "77")


def test_legacy_roster_shape_for_enrichment(source):
    rows = source.legacy_roster(LEAGUE_KEY)
    qb = rows[0]
    assert {"name", "position", "team", "status", "bye", "yahoo_projection", "opponent"} <= set(qb)
    assert (qb["name"], qb["position"], qb["team"], qb["opponent"]) == ("Caleb Williams", "QB", "CHI", "PHI")


def test_all_rosters_position_filter(source):
    teams = source.all_rosters(LEAGUE_KEY, "QB")["teams"]
    assert {t["team_key"] for t in teams} == {ME, OTHER}
    assert all("QB" in p["positions"] for t in teams for p in t["players"])
    flex = source.all_rosters(LEAGUE_KEY, "W/R/T")["teams"]
    assert {pos for t in flex for p in t["players"] for pos in p["positions"]} <= {"WR", "RB", "TE"}


def test_compare_teams_position_summary(source):
    result = source.compare_teams(LEAGUE_KEY, "4", "9")
    a, b = result["team_a"], result["team_b"]
    assert (a["team_key"], b["team_key"]) == (ME, OTHER)
    assert sum(v["count"] for v in a["position_summary"].values()) == 14
    assert "RB" in b["by_position"]


# ----------------------------------------------------------------------- matchups


def test_current_and_past_matchups(source):
    current = source.matchups(LEAGUE_KEY)
    assert current["week"] == 3 and len(current["matchups"]) == 5
    assert sum(m["involves_me"] for m in current["matchups"]) == 1
    week1 = source.matchups(LEAGUE_KEY, 1)["matchups"]
    assert all(m["winner_team_key"] for m in week1)
    missing = source.matchups(LEAGUE_KEY, 9)
    assert missing["matchups"] == [] and "weeks available" in missing["note"]


def test_my_matchup_includes_both_lineups(source):
    result = source.my_matchup(LEAGUE_KEY)
    assert result["opponent"]["team_key"] == "nfl.l.269337.t.3"
    assert result["my_roster"] and result["opponent_roster"] == []  # team 3's roster not in the fixture DB
    week1 = source.my_matchup(LEAGUE_KEY, 1)
    assert week1["matchup"]["winner_team_key"] == ME and "my_roster" not in week1


# ------------------------------------------------------------- available players


def test_available_players_filters_and_sorts(source):
    result = source.available_players(LEAGUE_KEY, "QB", "projected_rest_of_season", 5)
    values = [p["projected_rest_of_season"] for p in result["players"]]
    assert values == sorted(values, reverse=True) and len(values) <= 5
    assert all("QB" in p["positions"] for p in result["players"])
    defenses = source.available_players(LEAGUE_KEY, "DEF", count=100)
    assert defenses["total_available_matching"] == len(defenses["players"]) > 0
    assert result["scan_depth"] == ["O top 25"]  # fixture scan marked as capped


def test_trending_sort_is_honest(source):
    assert "not synced" in source.available_players(LEAGUE_KEY, sort="trending")["note"]


def test_legacy_waiver_shape(source):
    rows = source.legacy_waiver_players(LEAGUE_KEY, "all", "rank", 3)
    assert len(rows) == 3
    assert {"name", "position", "team", "owned_pct", "injury_status", "bye", "availability"} <= set(rows[0])


def test_search_players_across_rosters_and_waivers(source):
    rostered = source.search_players(LEAGUE_KEY, "caleb williams")["matches"]
    assert rostered[0]["ownership"] == "rostered" and rostered[0]["fantasy_team_key"] == ME
    available = source.search_players(LEAGUE_KEY, "GOFF")["matches"]
    assert available[0]["ownership"] == "waivers"
    accented = source.search_players(LEAGUE_KEY, "jamarr")["matches"]  # "Ja'Marr" via draft history
    assert any("Chase" in m["name"] for m in accented)


# ------------------------------------------------------------------------ history


def test_transactions_filters(source):
    everything = source.transactions(LEAGUE_KEY, limit=500)
    assert everything["total_matching"] > 25
    mine = source.transactions(LEAGUE_KEY, team_key="4", limit=500)["transactions"]
    assert mine and all(t["team_key"] == ME for t in mine)
    drops = source.transactions(LEAGUE_KEY, type="drop", limit=500)["transactions"]
    assert all("drop" in t["type"] for t in drops)
    by_player = source.transactions(LEAGUE_KEY, player="michael mayer")["transactions"]
    assert by_player and by_player[0]["players"][0]["name"] == "Michael Mayer"
    recent = source.transactions(LEAGUE_KEY, since="2026-09-20", limit=500)["transactions"]
    assert recent and all(t["timestamp"] >= "2026-09-20" for t in recent)


def test_faab_history_summaries(source):
    result = source.faab_bids(LEAGUE_KEY)
    summary = result["by_manager"]
    # Spending comes from transactions ("$N Waiver" adds), including uncontested claims.
    waiver_adds = [p for t in source.snapshot(LEAGUE_KEY).transactions
                   if t.team_key in summary for p in t.players if p.action == "add" and p.faab_bid is not None]
    assert sum(m["waiver_adds"] for m in summary.values()) == len(waiver_adds)
    assert sum(m["faab_spent"] for m in summary.values()) == sum(p.faab_bid for p in waiver_adds)
    assert len(result["contested_claims"]) == 4
    vele = next(c for c in result["contested_claims"] if c["name"] == "Devaughn Vele")
    assert [b["bid"] for b in vele["bids"]] == [18, 8, 7]


def test_draft_results_current_status(source):
    picks = source.draft_results(LEAGUE_KEY, "4")["picks"]
    assert len(picks) == 15 and all(p["team_key"] == ME for p in picks)
    statuses = {p["current_status"] for p in picks}
    assert "still on drafting team" in statuses


def test_player_history(source):
    history = source.player_history(LEAGUE_KEY, "40900")
    assert history["player_key"] == "nfl.p.40900"
    assert history["roster_history"][0]["team_key"] == ME
    assert history["drafted"] and history["drafted"]["team_key"] == ME


# ------------------------------------------------------------------ status/errors


def test_sync_status_reports_age_and_stale(source):
    status = source.sync_status(LEAGUE_KEY)
    assert status["status"] == "ok" and isinstance(status["age_hours"], float)
    assert "sync_yahoo_league.py --league-id 269337" in status["refresh_command"]
    source.store.save_snapshot(build_snapshot("2020-01-01T00:00:00+00:00"))  # an old sync becomes latest
    assert source.sync_status(LEAGUE_KEY)["stale"] is True


def test_missing_database_is_reported(tmp_path):
    empty = LocalLeagueSource(db_path=tmp_path / "missing.db")
    with pytest.raises(NoLocalData, match="No league database"):
        empty.snapshot(LEAGUE_KEY)
    assert empty.sync_status()["status"] == "no_data"


def test_cache_reloads_after_new_sync(source):
    assert source.league_info(LEAGUE_KEY)["your_team"]["faab_remaining"] == 50
    newer = build_snapshot("2026-09-23T21:00:00+00:00")
    newer.teams = [replace(t, faab_remaining=12.0) if t.is_mine else t for t in newer.teams]
    source.store.save_snapshot(newer)
    assert source.league_info(LEAGUE_KEY)["your_team"]["faab_remaining"] == 12


# ------------------------------------------------------------------------ handlers


def test_local_handlers_route_and_report_missing_data(source, tmp_path):
    pytest.importorskip("aiohttp")  # handler package imports the Yahoo API client
    from src.handlers import local_handlers

    local_handlers.set_source(source)
    try:
        run = lambda name, **args: asyncio.run(local_handlers.LOCAL_TOOL_HANDLERS[name](args))  # noqa: E731
        assert run("ff_get_standings", league_key=LEAGUE_KEY)["standings"]
        assert run("ff_get_roster", league_key=LEAGUE_KEY, data_level="basic")["team_key"] == ME
        assert run("ff_search_players", league_key=LEAGUE_KEY)["status"] == "error"
        assert run("ff_get_draft_rankings", league_key=LEAGUE_KEY)["status"] == "unavailable"
        assert run("ff_refresh_token")["status"] == "not_applicable"
        assert set(local_handlers.LOCAL_ONLY_TOOLS) <= set(local_handlers.LOCAL_TOOL_HANDLERS)

        local_handlers.set_source(LocalLeagueSource(db_path=tmp_path / "missing.db"))
        missing = run("ff_get_standings", league_key=LEAGUE_KEY)
        assert missing["status"] == "error" and "sync_yahoo_league.py" in missing["suggestion"]
    finally:
        local_handlers.set_source(None)
