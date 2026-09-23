"""Lineup optimization from synced league data.

Inputs come straight from the league: the roster slots in league settings, each player's
Yahoo projection for the current week, injury/roster status, and bye week. Expected
points = projection x availability factor (from status) x 0 if on bye. Slots are filled
best-first: position-specific slots, then flex slots (W/R/T etc.) with the best remaining
eligible players — optimal for Yahoo's nested flex eligibility.

Also reports lineup changes vs. the current lineup, risky starters, and waiver/free-agent
players who would beat a starter this week.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from src.models.league_data import AvailablePlayer, LeagueSnapshot, RosterEntry

NON_STARTING_SLOTS = {"BN", "IR"}

# Which player positions each flex slot accepts (Yahoo labels).
FLEX_SLOTS: Dict[str, Set[str]] = {
    "W/R/T": {"WR", "RB", "TE"},
    "W/R": {"WR", "RB"},
    "W/T": {"WR", "TE"},
    "R/T": {"RB", "TE"},
    "Q/W/R/T": {"QB", "WR", "RB", "TE"},
    "D": {"DL", "LB", "DB", "CB", "S", "DE", "DT"},
}

# Chance-to-play multipliers by Yahoo status code, per strategy. Statuses not listed
# as playable are treated as out. Unknown codes get UNKNOWN_STATUS_FACTOR and a note.
OUT_STATUSES = {"O", "IR", "IR-R", "PUP-P", "PUP-R", "NFI-R", "SUSP", "NA", "CEL", "COVID-19", "DNR", "EX", "RET"}
AVAILABILITY: Dict[str, Dict[Optional[str], float]] = {
    "conservative": {None: 1.0, "Q": 0.7, "D": 0.1, "NFI-A": 0.7},
    "balanced": {None: 1.0, "Q": 0.85, "D": 0.25, "NFI-A": 0.85},
    "aggressive": {None: 1.0, "Q": 0.95, "D": 0.4, "NFI-A": 0.95},
}
UNKNOWN_STATUS_FACTOR = 0.5
UPGRADE_MARGIN = 1.0  # expected points a waiver player must add to be suggested


@dataclass
class Scored:
    player_key: str
    name: str
    positions: List[str]
    current_slot: Optional[str]
    nfl_team: Optional[str]
    status: Optional[str]
    status_full: Optional[str]
    bye_week: Optional[int]
    projection: Optional[float]
    factor: float
    expected: float
    notes: List[str]
    game: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "player_key": self.player_key, "name": self.name, "positions": self.positions,
            "nfl_team": self.nfl_team, "status": self.status, "status_full": self.status_full,
            "projected_points": self.projection, "availability_factor": self.factor,
            "expected_points": round(self.expected, 2), "notes": self.notes, "game": self.game,
            "current_slot": self.current_slot,
        }


def slot_accepts(slot: str, positions: Iterable[str]) -> bool:
    positions = set(positions)
    return slot in positions or bool(FLEX_SLOTS.get(slot, set()) & positions)


def score_player(
    player_key: str, name: str, positions: List[str], status: Optional[str], status_full: Optional[str],
    bye_week: Optional[int], projection: Optional[float], week: Optional[int], strategy: str,
    current_slot: Optional[str] = None, nfl_team: Optional[str] = None, game: Optional[str] = None,
) -> Scored:
    factors = AVAILABILITY.get(strategy, AVAILABILITY["balanced"])
    notes: List[str] = []
    if status in OUT_STATUSES:
        factor = 0.0
        notes.append(f"{status_full or status}: not expected to play")
    elif status in factors:
        factor = factors[status]
        if status:
            notes.append(f"{status_full or status}: x{factor}")
    else:
        factor = UNKNOWN_STATUS_FACTOR
        notes.append(f"unrecognized status {status!r}: x{factor}")
    if week is not None and bye_week == week:
        factor = 0.0
        notes.append(f"on bye (week {week})")
    if projection is None:
        notes.append("no projection")
    expected = (projection or 0.0) * factor
    return Scored(player_key, name, positions, current_slot, nfl_team, status, status_full, bye_week,
                  projection, factor, expected, notes, game)


def _score_roster(players: Sequence[RosterEntry], week: Optional[int], strategy: str) -> List[Scored]:
    return [
        score_player(p.player_key, p.name, p.positions, p.status, p.status_full, p.bye_week, p.projected_points,
                     week, strategy, current_slot=p.slot, nfl_team=p.nfl_team, game=p.game)
        for p in players
    ]


def _score_available(players: Sequence[AvailablePlayer], week: Optional[int], strategy: str) -> List[Scored]:
    return [
        score_player(p.player_key, p.name, p.positions, p.status, p.status_full, p.bye_week, p.projected_week,
                     week, strategy, nfl_team=p.nfl_team, game=p.game)
        for p in players
    ]


def _starting_slots(roster_positions: Sequence[str]) -> List[str]:
    """Starting slots, position-specific first, then flex (most restrictive flex first)."""
    slots = [s for s in roster_positions if s not in NON_STARTING_SLOTS]
    return sorted(slots, key=lambda s: (s in FLEX_SLOTS, len(FLEX_SLOTS.get(s, ()))))


def assign(slots: Sequence[str], candidates: Sequence[Scored]) -> List[tuple]:
    """Fill slots best-first; returns [(slot, Scored | None)] in league slot order."""
    ranked = sorted(candidates, key=lambda c: -c.expected)
    used: Set[str] = set()
    filled: Dict[int, Optional[Scored]] = {}
    ordered = _starting_slots(slots)
    order_index = {i: slot for i, slot in enumerate(slots) if slot not in NON_STARTING_SLOTS}
    remaining = dict(order_index)
    for slot in ordered:
        index = next(i for i, s in remaining.items() if s == slot)
        del remaining[index]
        pick = next((c for c in ranked if c.player_key not in used and slot_accepts(slot, c.positions)), None)
        if pick is not None:
            used.add(pick.player_key)
        filled[index] = pick
    return [(order_index[i], filled[i]) for i in sorted(filled)]


def optimize_lineup(
    snapshot: LeagueSnapshot,
    team_key: str,
    strategy: str = "balanced",
    week: Optional[int] = None,
    include_waivers: bool = True,
) -> Dict[str, Any]:
    strategy = strategy if strategy in AVAILABILITY else "balanced"
    week = week or snapshot.league.current_week
    roster = next((r for r in snapshot.rosters if r.team_key == team_key), None)
    if roster is None:
        raise KeyError(f"No roster for {team_key}")
    slots = snapshot.settings.roster_positions
    scored = _score_roster(roster.players, week, strategy)
    lineup = assign(slots, scored)
    starters = {s.player_key for _, s in lineup if s is not None}

    current = [s for s in scored if s.current_slot not in NON_STARTING_SLOTS]
    current_total = sum(s.expected for s in current)
    total = sum(s.expected for _, s in lineup if s is not None)

    moves = []
    for s in scored:
        now_starting = s.current_slot not in NON_STARTING_SLOTS
        should_start = s.player_key in starters
        if should_start and not now_starting:
            moves.append({"action": "start", "player": s.name, "from_slot": s.current_slot,
                          "expected_points": round(s.expected, 2)})
        elif now_starting and not should_start:
            moves.append({"action": "bench", "player": s.name, "from_slot": s.current_slot,
                          "expected_points": round(s.expected, 2), "why": s.notes or ["lower expected points"]})

    warnings = []
    for slot, s in lineup:
        if s is None:
            warnings.append(f"No eligible player for {slot}")
        elif s.expected <= 0:
            warnings.append(f"{slot}: best option {s.name} projects 0 ({'; '.join(s.notes) or 'no projection'})")
        elif s.factor < 1:
            warnings.append(f"{slot}: {s.name} is a risk ({'; '.join(s.notes)})")

    result: Dict[str, Any] = {
        "team_key": team_key,
        "week": week,
        "strategy": strategy,
        "method": "Yahoo projection x availability (status/bye); position slots then flex",
        "lineup": [{"slot": slot, **(s.as_dict() if s else {"player": None})} for slot, s in lineup],
        "bench": [s.as_dict() for s in sorted(scored, key=lambda c: -c.expected) if s.player_key not in starters],
        "projected_total": round(total, 2),
        "current_lineup_total": round(current_total, 2),
        "gain_vs_current": round(total - current_total, 2),
        "changes": moves,
        "warnings": warnings,
        "availability_factors": {k or "healthy": v for k, v in AVAILABILITY[strategy].items()},
    }
    if include_waivers:
        result["waiver_upgrades"] = waiver_upgrades(snapshot, lineup, week, strategy)
    return result


def waiver_upgrades(snapshot: LeagueSnapshot, lineup: Sequence[tuple], week: Optional[int],
                    strategy: str, per_slot: int = 3) -> List[Dict[str, Any]]:
    """Available players who beat the weakest starter at a slot this week."""
    pool = _score_available(snapshot.available_players, week, strategy)
    availability = {p.player_key: (p.availability, p.waiver_until) for p in snapshot.available_players}
    suggestions = []
    seen_slots: Set[str] = set()
    for slot, starter in sorted(lineup, key=lambda item: item[1].expected if item[1] else -1):
        if slot in seen_slots:
            continue  # suggest once per slot type, against its weakest starter
        seen_slots.add(slot)
        baseline = starter.expected if starter else 0.0
        better = sorted(
            (c for c in pool if slot_accepts(slot, c.positions) and c.expected >= baseline + UPGRADE_MARGIN),
            key=lambda c: -c.expected,
        )[:per_slot]
        if not better:
            continue
        suggestions.append({
            "slot": slot,
            "replaces": starter.name if starter else None,
            "replaces_expected_points": round(baseline, 2),
            "candidates": [
                {**c.as_dict(), "availability": availability[c.player_key][0],
                 "waiver_until": availability[c.player_key][1],
                 "gain": round(c.expected - baseline, 2)}
                for c in better
            ],
        })
    return suggestions
