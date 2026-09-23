"""Tests for the league-aware lineup optimizer (src/analysis/lineup.py)."""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from league_fixtures import build_snapshot  # noqa: E402
from src.analysis.lineup import assign, optimize_lineup, score_player, slot_accepts  # noqa: E402
from src.models.league_data import RosterEntry  # noqa: E402

ME = "nfl.l.269337.t.4"
SLOTS = ["QB", "WR", "WR", "WR", "RB", "RB", "TE", "W/R/T", "W/R/T", "K", "DEF", "BN", "BN", "BN", "BN", "IR", "IR"]


def entry(name, pos, proj, slot="BN", status=None, bye=None):
    return RosterEntry(ME, f"nfl.p.{abs(hash(name)) % 10**6}", "0", name, slot, "XX", [pos], status,
                       None, bye, None, proj, None, None, None)


def scored(players, week=3, strategy="balanced"):
    return [score_player(p.player_key, p.name, p.positions, p.status, p.status_full, p.bye_week,
                         p.projected_points, week, strategy, current_slot=p.slot) for p in players]


# ---------------------------------------------------------------------- scoring


@pytest.mark.parametrize("status, strategy, factor", [
    (None, "balanced", 1.0), ("Q", "balanced", 0.85), ("D", "balanced", 0.25),
    ("Q", "conservative", 0.7), ("D", "aggressive", 0.4), ("O", "aggressive", 0.0),
    ("IR", "balanced", 0.0), ("SUSP", "balanced", 0.0), ("NA", "balanced", 0.0),
])
def test_availability_factor(status, strategy, factor):
    s = score_player("k", "P", ["WR"], status, None, None, 10.0, 3, strategy)
    assert s.factor == factor and s.expected == pytest.approx(10.0 * factor)


def test_bye_week_zeroes_expected_points():
    s = score_player("k", "P", ["WR"], None, None, 3, 15.0, 3, "balanced")
    assert s.expected == 0 and any("bye" in n for n in s.notes)


def test_unknown_status_is_flagged():
    s = score_player("k", "P", ["WR"], "ZZ", None, None, 10.0, 3, "balanced")
    assert s.factor == 0.5 and "unrecognized" in s.notes[0]


def test_flex_eligibility():
    assert slot_accepts("W/R/T", ["TE"]) and slot_accepts("W/R/T", ["RB"])
    assert not slot_accepts("W/R/T", ["QB"]) and not slot_accepts("WR", ["RB"])
    assert slot_accepts("Q/W/R/T", ["QB"])


# ------------------------------------------------------------------- assignment


def test_assignment_fills_positions_then_flex_with_best_remaining():
    players = [
        entry("QB1", "QB", 20), entry("WR1", "WR", 18), entry("WR2", "WR", 15), entry("WR3", "WR", 12),
        entry("WR4", "WR", 11), entry("RB1", "RB", 16), entry("RB2", "RB", 14), entry("RB3", "RB", 13),
        entry("TE1", "TE", 9), entry("TE2", "TE", 8), entry("K1", "K", 8), entry("D1", "DEF", 6),
        entry("RB4", "RB", 2),
    ]
    lineup = assign(SLOTS, scored(players))
    by_slot = [(slot, s.name) for slot, s in lineup]
    assert by_slot[:7] == [("QB", "QB1"), ("WR", "WR1"), ("WR", "WR2"), ("WR", "WR3"),
                           ("RB", "RB1"), ("RB", "RB2"), ("TE", "TE1")]
    assert {name for slot, name in by_slot if slot == "W/R/T"} == {"RB3", "WR4"}  # best remaining RB/WR/TE
    assert "RB4" not in {name for _, name in by_slot}


def test_injured_starter_replaced_by_healthy_backup():
    players = [entry("Star", "RB", 20, slot="RB", status="O"), entry("Backup", "RB", 8),
               entry("RB2", "RB", 12, slot="RB")]
    lineup = assign(["RB", "RB"], scored(players))
    assert {s.name for _, s in lineup} == {"RB2", "Backup"}


def test_empty_slot_when_no_eligible_player():
    lineup = assign(["QB", "K"], scored([entry("QB1", "QB", 20)]))
    assert lineup[1] == ("K", None)


# ------------------------------------------------------------------ real fixture


@pytest.fixture(scope="module")
def snapshot():
    return build_snapshot()


def test_optimize_fixture_team(snapshot):
    result = optimize_lineup(snapshot, ME)
    lineup = {row["slot"] for row in result["lineup"]}
    assert lineup == {"QB", "WR", "RB", "TE", "W/R/T", "K", "DEF"}
    assert len(result["lineup"]) == 11
    qb = next(r for r in result["lineup"] if r["slot"] == "QB")
    assert qb["name"] == "Caleb Williams" and qb["expected_points"] == 0  # Doubtful, projected 0
    assert any("QB" in w and "projects 0" in w for w in result["warnings"])
    assert result["projected_total"] >= result["current_lineup_total"]
    moved_in = {c["player"] for c in result["changes"] if c["action"] == "start"}
    moved_out = {c["player"] for c in result["changes"] if c["action"] == "bench"}
    assert len(moved_in) == len(moved_out)


def test_waiver_upgrades_beat_weakest_starter(snapshot):
    # Give available players a current-week projection (fixture lists are rest-of-season views).
    boosted = [replace(p, projected_week=(25.0 if "QB" in p.positions else 1.0)) for p in snapshot.available_players]
    result = optimize_lineup(replace(snapshot, available_players=boosted), ME)
    qb = next(u for u in result["waiver_upgrades"] if u["slot"] == "QB")
    assert qb["replaces"] == "Caleb Williams"
    assert all(c["expected_points"] >= qb["replaces_expected_points"] + 1 for c in qb["candidates"])
    assert all(c["availability"] in ("waivers", "free_agent") for c in qb["candidates"])
    assert not any(u["slot"] == "K" for u in result["waiver_upgrades"])  # 1-point kickers don't beat a starter


def test_strategy_changes_risk_tolerance(snapshot):
    team9 = "nfl.l.269337.t.9"
    conservative = optimize_lineup(snapshot, team9, strategy="conservative", include_waivers=False)
    aggressive = optimize_lineup(snapshot, team9, strategy="aggressive", include_waivers=False)
    assert aggressive["projected_total"] >= conservative["projected_total"]
    assert "waiver_upgrades" not in conservative


def test_unknown_team_raises(snapshot):
    with pytest.raises(KeyError):
        optimize_lineup(snapshot, "nfl.l.269337.t.3")
