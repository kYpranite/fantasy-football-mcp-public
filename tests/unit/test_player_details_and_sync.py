"""Offline tests for Sleeper player details and the background sync trigger."""

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from league_fixtures import LEAGUE_KEY, build_snapshot  # noqa: E402
from src.datasource import sync_trigger  # noqa: E402
from src.datasource.local_source import LocalLeagueSource  # noqa: E402
from src.datasource.player_details import (  # noqa: E402
    PLAYERS_URL,
    PlayerDetailsError,
    SleeperPlayerDetails,
    normalize_name,
)
from src.storage.db import connect  # noqa: E402
from src.storage.repository import LeagueStore  # noqa: E402

SLEEPER_PLAYERS = {
    "11560": {"player_id": "11560", "full_name": "Caleb Williams", "position": "QB", "fantasy_positions": ["QB"],
              "team": "CHI", "injury_status": "Out", "injury_body_part": "Hamstring",
              "depth_chart_position": "QB", "depth_chart_order": 3},
    "999": {"player_id": "999", "full_name": "Caleb Williams", "position": "WR", "fantasy_positions": ["WR"],
            "team": "FA"},
    "6786": {"player_id": "6786", "full_name": "CeeDee Lamb", "position": "WR", "fantasy_positions": ["WR"],
             "team": "DAL", "yahoo_id": 32687},
    "NE": {"player_id": "NE", "full_name": None, "first_name": "New England", "last_name": "Patriots",
           "position": "DEF", "team": "NE"},
}
WEEKS = {
    "1": {"date": "2026-09-13", "opponent": "NYG", "stats": {"gp": 1, "pts_ppr": 15.4, "off_snp": 47,
                                                               "tm_off_snp": 58, "rec_tgt": 8, "rec": 5}},
    "2": {"date": "2026-09-20", "opponent": "WAS", "stats": {"gp": 1, "pts_ppr": 35.3, "off_snp": 41,
                                                               "tm_off_snp": 55, "rec_tgt": 9, "rec": 8,
                                                               "pos_rank_ppr": 999}},
}


def fake_fetch(url):
    return SLEEPER_PLAYERS if url == PLAYERS_URL else WEEKS


@pytest.fixture
def details(tmp_path):
    return SleeperPlayerDetails(cache_path=tmp_path / "players.json", fetch=fake_fetch)


# --------------------------------------------------------------- player details


def test_normalize_name():
    assert normalize_name("James Cook III") == "james cook"
    assert normalize_name("Ja'Marr Chase") == "jamarr chase"
    assert normalize_name("Amon-Ra St. Brown") == "amon ra st brown"


def test_match_prefers_yahoo_id_then_name_position(details):
    assert details.match("32687", "CeeDee Lamb", "WR", "DAL")[1] == "yahoo_id"
    player, method = details.match("40900", "Caleb Williams", "QB", "CHI")  # two Sleeper Calebs
    assert (player["player_id"], method) == ("11560", "name_position")
    assert details.match("100017", "Patriots", "DEF", "NE")[1] == "team_defense"
    assert details.match("1", "Nobody Real", "WR", "DAL")[0] is None


def test_players_db_cached_on_disk(details, tmp_path):
    details.players()
    assert (tmp_path / "players.json").exists()
    offline = SleeperPlayerDetails(cache_path=tmp_path / "players.json",
                                   fetch=lambda url: (_ for _ in ()).throw(AssertionError("no network")))
    assert "6786" in offline.players()


def test_game_log_and_summary(details):
    result = details.details("32687", "CeeDee Lamb", "WR", "DAL", 2026)
    log = result["game_log"]
    assert [g["week"] for g in log] == [1, 2]
    assert log[0]["snap_share"] == pytest.approx(47 / 58, abs=1e-3) and log[0]["rec_tgt"] == 8
    assert log[1]["pos_rank_ppr"] is None  # Sleeper's 999 placeholder
    assert result["season_summary"]["avg_pts_ppr"] == pytest.approx(25.35)
    assert "not this league's" in result["points_note"]


def test_unmatched_player_raises(details):
    with pytest.raises(PlayerDetailsError, match="Could not match"):
        details.details("1", "Nobody Real", "WR", "DAL", 2026)


def test_local_player_details_combines_league_and_sleeper(tmp_path, details):
    db = tmp_path / "league.db"
    store = LeagueStore(connect(db))
    store.save_snapshot(build_snapshot())
    source = LocalLeagueSource(db_path=db, store=store)
    result = source.player_details(LEAGUE_KEY, "caleb williams", details_client=details)
    assert result["status"] == "success" and result["league_context"]["status_full"] == "Doubtful"
    assert result["sleeper"]["profile"]["injury_status"] == "Out"  # sources disagree; both shown
    assert source.player_details(LEAGUE_KEY, "williams", details_client=details)["status"] == "ambiguous"
    assert source.player_details(LEAGUE_KEY, "40900", details_client=details)["name"] == "Caleb Williams"
    store.conn.close()


# ---------------------------------------------------------------------- sync


class FakeProcess:
    pid = 4321


def test_background_sync_starts_detached_with_token(tmp_path):
    calls = []
    result = sync_trigger.start_background_sync(
        "269337", mode="quick", lock_path=tmp_path / "sync.lock", log_path=tmp_path / "sync.log",
        popen=lambda cmd, **kw: calls.append((cmd, kw)) or FakeProcess(),
    )
    assert result["status"] == "started"
    cmd, kwargs = calls[0]
    assert cmd[-3:] == ["--league-id", "269337", "--no-history"]
    lock = json.loads((tmp_path / "sync.lock").read_text())
    assert kwargs["env"][sync_trigger.TOKEN_ENV] == lock["token"]
    assert kwargs["stdout"] is not sys.stdout  # never the MCP protocol stream


def test_guards(tmp_path):
    lock, log = tmp_path / "sync.lock", tmp_path / "sync.log"
    popen = lambda cmd, **kw: FakeProcess()  # noqa: E731
    now = time.time()
    assert sync_trigger.start_background_sync("1", mode="bogus", lock_path=lock, log_path=log,
                                              popen=popen)["status"] == "error"
    assert sync_trigger.start_background_sync("1", last_synced_epoch=now - 60, lock_path=lock, log_path=log,
                                              popen=popen)["status"] == "recently_synced"
    assert sync_trigger.start_background_sync("1", last_attempt_epoch=now - 30, lock_path=lock, log_path=log,
                                              popen=popen)["status"] == "recently_attempted"
    assert sync_trigger.start_background_sync("1", last_synced_epoch=now - 60, force=True, lock_path=lock,
                                              log_path=log, popen=popen)["status"] == "started"
    assert sync_trigger.start_background_sync("1", force=True, lock_path=lock, log_path=log,
                                              popen=popen)["status"] == "already_running"


def test_sync_lock_token_handoff_and_conflicts(tmp_path, monkeypatch):
    lock = tmp_path / "sync.lock"
    lock.write_text(json.dumps({"token": "abc", "started_at": time.time()}))
    monkeypatch.setenv(sync_trigger.TOKEN_ENV, "abc")
    with sync_trigger.sync_lock("1", "full", lock_path=lock):  # the launched child may take its own lock
        assert lock.exists()
    assert not lock.exists()
    lock.write_text(json.dumps({"token": "someone-else", "started_at": time.time()}))
    with pytest.raises(sync_trigger.SyncInProgress):
        with sync_trigger.sync_lock("1", "full", lock_path=lock):
            pass
    lock.write_text(json.dumps({"token": "old", "started_at": time.time() - 3600}))  # stale → ignored
    with sync_trigger.sync_lock("1", "full", lock_path=lock):
        pass
