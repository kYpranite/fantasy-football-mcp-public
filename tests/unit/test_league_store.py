"""Tests for the SQLite league store, using snapshots built from sanitized fixtures."""

import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.extractors.yahoo_web import parsers  # noqa: E402
from src.models.league_data import League, LeagueSnapshot, PlayerScan  # noqa: E402
from src.storage.db import connect, migrate, schema_version  # noqa: E402
from src.storage.repository import LeagueStore  # noqa: E402
from src.storage.schema import SCHEMA_VERSION  # noqa: E402

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
    available = [p for name in ("players_O", "players_DEF") for p, _ in parsers.parse_player_list(fixture(name)).rows]
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


@pytest.fixture
def store(tmp_path):
    conn = connect(tmp_path / "league.db")
    yield LeagueStore(conn)
    conn.close()


def count(store, table):
    return store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# -------------------------------------------------------------------------- schema


def test_migrations_create_schema_and_are_idempotent(tmp_path):
    path = tmp_path / "league.db"
    conn = connect(path)
    assert schema_version(conn) == SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()
    conn = connect(path)  # reopening applies nothing new
    assert migrate(conn) == SCHEMA_VERSION
    conn.close()


def test_newer_database_than_code_is_refused(tmp_path):
    path = tmp_path / "league.db"
    connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match="newer than this code"):
        connect(path)


# ------------------------------------------------------------------ save and load


def test_round_trip_preserves_current_state(store):
    snapshot = build_snapshot()
    run_id = store.save_snapshot(snapshot)
    loaded = store.load_snapshot(LEAGUE_KEY)
    assert store.latest_run_id(LEAGUE_KEY) == run_id
    original, restored = snapshot.to_dict(), loaded.to_dict()
    for key in ("captured_at", "source", "league", "settings", "teams", "standings", "rosters",
                "matchups", "available_players", "player_scans", "matchup_history", "warnings"):
        assert restored[key] == original[key], key


def test_round_trip_preserves_history_by_key(store):
    snapshot = build_snapshot()
    store.save_snapshot(snapshot)
    loaded = store.load_snapshot(LEAGUE_KEY)
    # Names can differ (players table keeps one name; Yahoo varies DEF labels), keys must not.
    strip = lambda items: [(t.transaction_id, t.type, t.team_key, t.timestamp,  # noqa: E731
                            [(p.player_key, p.action, p.detail, p.faab_bid) for p in t.players]) for t in items]
    assert sorted(strip(loaded.transactions)) == sorted(strip(snapshot.transactions))
    assert [(c.player_key, c.winning_bid, c.awarded_team_key, [(b.team_key, b.bid, b.result) for b in c.bids])
            for c in loaded.waiver_claims] == \
           [(c.player_key, c.winning_bid, c.awarded_team_key, [(b.team_key, b.bid, b.result) for b in c.bids])
            for c in snapshot.waiver_claims]
    assert [(d.overall, d.team_key, d.player_key, d.nfl_team, d.position) for d in loaded.draft_picks] == \
           [(d.overall, d.team_key, d.player_key, d.nfl_team, d.position) for d in snapshot.draft_picks]


def test_resync_does_not_duplicate_history(store):
    store.save_snapshot(build_snapshot())
    before = {t: count(store, t) for t in ("transactions", "transaction_players", "waiver_claims",
                                           "waiver_bids", "draft_picks", "matchups", "players")}
    store.save_snapshot(build_snapshot("2026-09-23T21:00:00+00:00"))
    after = {t: count(store, t) for t in before}
    assert after == before
    assert count(store, "sync_runs") == 2
    assert count(store, "roster_entries") == 2 * sum(len(r.players) for r in build_snapshot().rosters)


def test_new_history_is_appended(store):
    snapshot = build_snapshot()
    older = replace(snapshot, transactions=snapshot.transactions[5:])
    store.save_snapshot(older)
    assert count(store, "transactions") == len(snapshot.transactions) - 5
    store.save_snapshot(snapshot)
    assert count(store, "transactions") == len(snapshot.transactions)


# ------------------------------------------------------------ failure and history


def test_failed_save_leaves_previous_run_current(store):
    good = build_snapshot()
    good_run = store.save_snapshot(good)
    bad = build_snapshot("2026-09-23T21:00:00+00:00")
    duplicate = bad.rosters[0].players[0]
    bad.rosters[1].players.append(replace(duplicate, team_key=bad.rosters[1].team_key))  # same player twice
    bad.transactions.append(replace(bad.transactions[0], transaction_id="brand-new-tx"))
    with pytest.raises(sqlite3.IntegrityError):
        store.save_snapshot(bad)
    assert store.latest_run_id(LEAGUE_KEY) == good_run
    assert count(store, "sync_runs") == 1
    assert store.conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE transaction_id = 'brand-new-tx'").fetchone()[0] == 0
    assert store.load_snapshot(LEAGUE_KEY).to_dict()["rosters"] == good.to_dict()["rosters"]


def test_record_failed_sync_does_not_change_current(store):
    good_run = store.save_snapshot(build_snapshot())
    store.record_failed_sync(LEAGUE_KEY, "yahoo_web", "2026-09-23T00:00:00+00:00", "Login required")
    assert store.latest_run_id(LEAGUE_KEY) == good_run
    runs = store.list_runs(LEAGUE_KEY)
    assert [r["status"] for r in runs] == ["failed", "committed"]
    assert runs[0]["error"] == "Login required"


def test_player_roster_history_shows_team_change(store):
    first = build_snapshot()
    store.save_snapshot(first)
    moved = build_snapshot("2026-09-29T21:00:00+00:00")
    player = moved.rosters[0].players.pop(3)
    moved.rosters[1].players.append(replace(player, team_key=moved.rosters[1].team_key, slot="BN"))
    store.save_snapshot(moved)
    history = store.player_roster_history(player.player_key)
    assert [(h["team_key"], h["slot"]) for h in history] == [
        (first.rosters[0].team_key, player.slot),
        (moved.rosters[1].team_key, "BN"),
    ]


def test_current_views_follow_latest_run(store):
    store.save_snapshot(build_snapshot())
    second = build_snapshot("2026-09-23T21:00:00+00:00")
    second.teams[0] = replace(second.teams[0], faab_remaining=12.0)
    run_id = store.save_snapshot(second)
    rows = store.conn.execute(
        "SELECT run_id, faab_remaining FROM current_teams WHERE team_key = ?", (second.teams[0].team_key,)
    ).fetchall()
    assert [(r["run_id"], r["faab_remaining"]) for r in rows] == [(run_id, 12.0)]
    names = store.conn.execute("SELECT name FROM current_rosters WHERE team_key = 'nfl.l.269337.t.4'").fetchall()
    assert "Caleb Williams" in {r["name"] for r in names}


def test_load_snapshot_empty_database(store):
    assert store.load_snapshot(LEAGUE_KEY) is None
    assert store.latest_run_id(LEAGUE_KEY) is None
