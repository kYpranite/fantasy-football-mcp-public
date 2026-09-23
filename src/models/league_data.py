"""Normalized league data produced by extractors and consumed by data sources.

Source-agnostic: the Yahoo web extractor fills these today; a Yahoo API source can
fill the same shapes later. Keys follow Yahoo Fantasy API conventions so the two
sources stay interchangeable:

    league_key  nfl.l.<league_id>
    team_key    nfl.l.<league_id>.t.<team_id>
    player_key  nfl.p.<player_id>

Missing/unknown optional values are ``None`` rather than guessed defaults.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

GAME_CODE = "nfl"


def league_key(league_id: str) -> str:
    return f"{GAME_CODE}.l.{league_id}"


def team_key(league_id: str, team_id: str) -> str:
    return f"{GAME_CODE}.l.{league_id}.t.{team_id}"


def player_key(player_id: str) -> str:
    return f"{GAME_CODE}.p.{player_id}"


@dataclass
class ScoringRule:
    category: str  # e.g. "Offense", "Kickers", "Defense/Special Teams"
    stat: str  # e.g. "Passing Yards", "Points Allowed 7-13 points"
    raw_value: str  # as shown by Yahoo, e.g. "25 yards per point", "-2"
    points: Optional[float] = None  # points per occurrence, when a flat value
    yards_per_point: Optional[float] = None  # when expressed as "N yards per point"
    yahoo_default: Optional[str] = None  # shown only when the league overrides it


@dataclass
class PlayoffSettings:
    raw: Optional[str] = None
    num_teams: Optional[int] = None
    weeks: List[int] = field(default_factory=list)
    tie_breaker: Optional[str] = None
    reseeding: Optional[bool] = None


@dataclass
class LeagueSettings:
    raw: Dict[str, str]  # every label → value on the settings page, verbatim
    max_teams: Optional[int] = None
    scoring_type: Optional[str] = None
    draft_type: Optional[str] = None
    draft_time: Optional[str] = None
    start_scoring_week: Optional[int] = None
    roster_positions: List[str] = field(default_factory=list)
    waiver_type: Optional[str] = None
    waiver_time: Optional[str] = None
    uses_faab: Optional[bool] = None
    trade_end_date: Optional[str] = None
    trade_review: Optional[str] = None
    max_trades_season: Optional[str] = None
    max_acquisitions_season: Optional[str] = None
    max_acquisitions_week: Optional[str] = None
    divisions: Optional[bool] = None
    fractional_points: Optional[bool] = None
    negative_points: Optional[bool] = None
    playoffs: PlayoffSettings = field(default_factory=PlayoffSettings)
    scoring: List[ScoringRule] = field(default_factory=list)


@dataclass
class League:
    league_key: str
    league_id: str
    name: str
    season: Optional[int]
    current_week: Optional[int]
    num_teams: int
    url: str


@dataclass
class Team:
    team_key: str
    team_id: str
    name: str
    is_mine: bool = False
    managers: List[str] = field(default_factory=list)  # display names as Yahoo shows them
    is_commissioner: bool = False
    faab_remaining: Optional[float] = None
    waiver_priority: Optional[int] = None
    moves: Optional[int] = None
    trades: Optional[int] = None
    last_activity: Optional[str] = None  # Yahoo's display string, league-local time


@dataclass
class Standing:
    team_key: str
    rank: Optional[int]
    wins: int
    losses: int
    ties: int
    points_for: Optional[float]
    points_against: Optional[float]
    streak: Optional[str] = None


@dataclass
class RosterEntry:
    team_key: str
    player_key: str
    player_id: str
    name: str
    slot: str  # roster slot currently occupied: QB, RB, W/R/T, BN, IR, ...
    nfl_team: Optional[str] = None
    positions: List[str] = field(default_factory=list)  # fantasy eligibility, e.g. ["RB", "WR"]
    status: Optional[str] = None  # injury/status code: Q, D, O, IR, ...
    status_full: Optional[str] = None
    bye_week: Optional[int] = None
    fantasy_points: Optional[float] = None  # for the week shown on the team page
    projected_points: Optional[float] = None
    percent_started: Optional[float] = None
    percent_rostered: Optional[float] = None
    game: Optional[str] = None  # e.g. "Sun 1:00 pm @ Jax"

    @property
    def is_starter(self) -> bool:
        return self.slot not in ("BN", "IR")


@dataclass
class TeamRoster:
    team_key: str
    week: Optional[int]
    players: List[RosterEntry]
    empty_slots: Dict[str, int] = field(default_factory=dict)


@dataclass
class LeagueSnapshot:
    """Core league state captured by one extraction run."""

    captured_at: str  # ISO-8601 UTC
    source: str  # "yahoo_web" | "yahoo_api"
    league: League
    settings: LeagueSettings
    teams: List[Team]
    standings: List[Standing]
    rosters: List[TeamRoster]
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def team(self, team_key: str) -> Optional[Team]:
        return next((t for t in self.teams if t.team_key == team_key), None)

    @property
    def my_team(self) -> Optional[Team]:
        return next((t for t in self.teams if t.is_mine), None)
