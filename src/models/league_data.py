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
class MatchupSide:
    team_key: str
    points: Optional[float] = None
    projected_points: Optional[float] = None


@dataclass
class Matchup:
    week: int
    status: Optional[str]  # Yahoo's label, e.g. "Not started yet", "In progress", "Final"
    teams: List[MatchupSide]  # two sides, in Yahoo's display order

    @property
    def team_keys(self) -> List[str]:
        return [side.team_key for side in self.teams]

    @property
    def winner_team_key(self) -> Optional[str]:
        """Higher scorer once Yahoo marks the week final; None if unfinished or tied."""
        if not self.status or "final" not in self.status.lower() or len(self.teams) != 2:
            return None
        a, b = self.teams
        if a.points is None or b.points is None or a.points == b.points:
            return None
        return a.team_key if a.points > b.points else b.team_key


@dataclass
class AvailablePlayer:
    """A player not on any fantasy roster (free agent or on waivers)."""

    player_key: str
    player_id: str
    name: str
    availability: str  # "free_agent" | "waivers" | raw Yahoo label if unrecognized
    waiver_until: Optional[str] = None  # Yahoo's date label, e.g. "Sep 23"
    nfl_team: Optional[str] = None
    positions: List[str] = field(default_factory=list)
    status: Optional[str] = None
    status_full: Optional[str] = None
    bye_week: Optional[int] = None
    games_played: Optional[int] = None
    percent_rostered: Optional[float] = None
    preseason_rank: Optional[int] = None
    current_rank: Optional[int] = None  # Yahoo "Actual" rank for the primary view
    projected_week: Optional[float] = None  # projected points, current week
    projected_rest_of_season: Optional[float] = None
    season_points: Optional[float] = None
    game: Optional[str] = None


@dataclass
class PlayerScan:
    """How far one available-player list was paged, so truncation is explicit."""

    position_group: str  # Yahoo pos filter: "O", "K", "DEF"
    view: str  # Yahoo stat1 view, e.g. "S_PSR_2026"
    pages: int
    players: int
    reached_end: bool  # False → stopped at the depth limit; deeper players exist


@dataclass
class TransactionPlayer:
    player_key: str
    player_id: str
    name: str
    action: str  # "add" | "drop" | "trade" | other Yahoo action label
    detail: Optional[str] = None  # Yahoo's source/destination text: "Free Agent", "$18 Waiver", "To Waivers"
    faab_bid: Optional[float] = None  # winning bid when added via FAAB waiver claim
    nfl_team: Optional[str] = None
    positions: List[str] = field(default_factory=list)


@dataclass
class Transaction:
    transaction_id: str  # stable hash of team, time, and players (Yahoo shows no id)
    type: str  # "add" | "drop" | "add/drop" | "trade" | ...
    team_key: Optional[str]  # team that made the move
    timestamp: Optional[str]  # ISO-8601 local time (year inferred from season)
    timestamp_raw: str  # Yahoo's label, e.g. "Sep 22, 6:04 pm"
    players: List[TransactionPlayer]


@dataclass
class WaiverBid:
    team_key: Optional[str]
    bid: Optional[float]
    result: str  # "won" or Yahoo's reason, e.g. "Lower Offer", "Lower waiver priority"


@dataclass
class WaiverClaim:
    """A processed FAAB waiver claim, including every losing bid."""

    player_key: str
    player_id: str
    name: str
    awarded_team_key: Optional[str]
    winning_bid: Optional[float]
    timestamp: Optional[str]
    timestamp_raw: str
    bids: List[WaiverBid]  # winning bid first, then losing bids in Yahoo's order
    nfl_team: Optional[str] = None
    positions: List[str] = field(default_factory=list)


@dataclass
class DraftPick:
    round: int
    pick: int  # pick number within the round
    overall: int
    team_key: Optional[str]  # None when the drafting team name no longer matches a team
    team_name: str  # as shown on the draft results page
    player_key: str
    player_id: str
    name: str
    nfl_team: Optional[str] = None
    position: Optional[str] = None


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
    matchups: List[Matchup] = field(default_factory=list)  # current week
    available_players: List[AvailablePlayer] = field(default_factory=list)
    player_scans: List[PlayerScan] = field(default_factory=list)
    matchup_history: List[Matchup] = field(default_factory=list)  # weeks before current
    transactions: List[Transaction] = field(default_factory=list)  # newest first
    waiver_claims: List[WaiverClaim] = field(default_factory=list)  # newest first
    draft_picks: List[DraftPick] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def team(self, team_key: str) -> Optional[Team]:
        return next((t for t in self.teams if t.team_key == team_key), None)

    @property
    def my_team(self) -> Optional[Team]:
        return next((t for t in self.teams if t.is_mine), None)
