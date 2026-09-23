"""Drive the authenticated browser through league pages and build a LeagueSnapshot.

Navigation and throttling live here; HTML interpretation lives in ``parsers``.
Extraction is all-or-nothing: any page or validation failure raises
``ExtractionError`` so callers never treat a partial league as complete.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from src.extractors.yahoo_web import parsers
from src.extractors.yahoo_web.parsers import open_slots
from src.extractors.yahoo_web.session import AuthRequired, is_auth_url, league_url, redact_url
from src.models.league_data import (
    AvailablePlayer,
    League,
    LeagueSnapshot,
    PlayerScan,
    TeamRoster,
    league_key,
)


class ExtractionError(RuntimeError):
    """Extraction failed or produced data that did not pass validation."""


PAGE_SIZE = 25  # Yahoo player list / transactions page size
MAX_HISTORY_PAGES = 40  # safety cap per paged history list (1,000 rows)
# Yahoo "pos" filter → default number of available players to collect per view.
DEFAULT_PLAYER_DEPTH: Dict[str, int] = {"O": 100, "K": 25, "DEF": 50}


def player_views(season: int, week: int) -> List[Tuple[str, str]]:
    """(AvailablePlayer field, Yahoo stat1 view). The first view sets list order."""
    return [
        ("projected_rest_of_season", f"S_PSR_{season}"),
        ("projected_week", f"S_PW_{week}"),
        ("season_points", f"S_S_{season}"),
    ]


class YahooWebExtractor:
    def __init__(
        self,
        page,
        league_id: str,
        delay_s: float = 2.0,
        log: Callable[[str], None] = print,
        on_page: Optional[Callable[[str, str], None]] = None,
    ):
        """``on_page(name, html)`` is called for every fetched page (debug snapshots)."""
        self.page = page
        self.league_id = str(league_id)
        self.delay_s = delay_s
        self.log = log
        self.on_page = on_page
        self._fetched = 0

    def fetch(self, name: str, path: str = "") -> str:
        """Load one league page and return its HTML, failing loudly on auth/HTTP errors."""
        if self._fetched:
            time.sleep(self.delay_s)
        self._fetched += 1
        url = f"{league_url(self.league_id)}/{path}" if path else league_url(self.league_id)
        self.log(f"  fetching {name}: {redact_url(url)}")
        response = self.page.goto(url, wait_until="domcontentloaded")
        if is_auth_url(self.page.url):
            raise AuthRequired(
                "Yahoo session expired. Run "
                f"'python utils/yahoo_browser_login.py --league-id {self.league_id} --manual-login' and log in."
            )
        if response is not None and response.status >= 400:
            raise ExtractionError(f"{name}: HTTP {response.status} for {redact_url(url)}")
        html = self.page.content()
        if self.on_page:
            self.on_page(name, html)
        return html

    def _parse(self, name: str, parse: Callable, *args):
        try:
            return parse(*args)
        except parsers.ParseError as exc:
            raise ExtractionError(f"{name}: {exc}") from exc

    def extract(
        self, player_depth: Optional[Dict[str, int]] = DEFAULT_PLAYER_DEPTH, include_history: bool = True
    ) -> LeagueSnapshot:
        """Full extraction.

        Current state (must all succeed): league metadata, settings, teams/managers,
        standings, every roster, current-week matchups, and (unless ``player_depth`` is
        None) available players. History (``include_history``): previous weeks' matchups,
        transactions, FAB offers, draft results — each part that fails becomes a warning
        instead of failing the sync.
        """
        captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        home_html = self.fetch("home")
        home = self._parse("league home", parsers.parse_league_home, home_html, self.league_id)
        matchups = self._parse("matchups", parsers.parse_matchups, home_html, self.league_id)
        settings = self._parse("settings", parsers.parse_settings, self.fetch("settings", "settings"))
        managers = self._parse(
            "managers", parsers.parse_managers, self.fetch("managers", "teams"), self.league_id
        )
        teams = parsers.merge_teams(home, managers)

        rosters: List[TeamRoster] = []
        for team in teams:
            html = self.fetch(f"team {team.team_id} roster", team.team_id)
            rosters.append(
                self._parse(
                    f"team {team.team_id} roster",
                    parsers.parse_team_roster,
                    html,
                    self.league_id,
                    team.team_id,
                    home.current_week,
                )
            )

        for roster in rosters:
            roster.empty_slots, _ = parsers.open_slots(settings.roster_positions, roster.players)

        snapshot = LeagueSnapshot(
            captured_at=captured_at,
            source="yahoo_web",
            league=League(
                league_key=league_key(self.league_id),
                league_id=self.league_id,
                name=settings.raw.get("League Name") or home.name or "",
                season=home.season,
                current_week=home.current_week,
                num_teams=len(teams),
                url=league_url(self.league_id),
            ),
            settings=settings,
            teams=teams,
            standings=[standing for _, _, standing in home.standings],
            rosters=rosters,
            matchups=matchups,
        )
        if player_depth and home.season and home.current_week:
            snapshot.available_players, snapshot.player_scans = self.extract_available_players(
                home.season, home.current_week, player_depth
            )
        history_warnings: List[str] = []
        if include_history and home.season and home.current_week:
            history_warnings = self.extract_history(snapshot)
        errors, snapshot.warnings = validate_snapshot(snapshot)
        snapshot.warnings = history_warnings + snapshot.warnings
        if errors:
            raise ExtractionError("Snapshot failed validation:\n  - " + "\n  - ".join(errors))
        return snapshot


    def extract_available_players(
        self, season: int, week: int, depth: Dict[str, int]
    ) -> Tuple[List[AvailablePlayer], List[PlayerScan]]:
        """Page through available players (free agents + waivers) for each group and view.

        Views are merged per player: the first view fixes the order, later views fill in
        their own value (and add players that only rank highly in that view).
        """
        merged: Dict[str, AvailablePlayer] = {}
        scans: List[PlayerScan] = []
        for group, limit in depth.items():
            for field_name, view in player_views(season, week):
                rows, scan = self._scan_player_list(group, view, limit)
                scans.append(scan)
                for player, value in rows:
                    entry = merged.get(player.player_id)
                    if entry is None:
                        entry = merged[player.player_id] = replace(player)
                    setattr(entry, field_name, value)
        return list(merged.values()), scans

    def extract_history(self, snapshot: LeagueSnapshot) -> List[str]:
        """Fill history fields on ``snapshot``; return warnings for parts that failed."""
        season, week = snapshot.league.season, snapshot.league.current_week
        warnings: List[str] = []

        def attempt(label: str, action: Callable[[], None]) -> None:
            try:
                action()
            except (ExtractionError, parsers.ParseError) as exc:
                warnings.append(f"{label} not collected: {exc}")

        def past_matchups() -> None:
            history = []
            for past in range(1, week):
                name = f"week {past} matchups"
                html = self.fetch(name, f"?matchup_week={past}&module=matchups&lhst=matchups")
                matchups = self._parse(name, parsers.parse_matchups, html, self.league_id)
                if {m.week for m in matchups} != {past}:
                    raise ExtractionError(f"{name}: page showed week {sorted({m.week for m in matchups})}")
                history.extend(matchups)
            snapshot.matchup_history = history

        def transactions() -> None:
            rows, capped = self._paged("transactions", "transactions?transactionsfilter=all",
                                       parsers.parse_transactions, season)
            unique = {t.transaction_id: t for t in rows}
            snapshot.transactions = list(unique.values())
            if capped:
                warnings.append(f"transactions capped at {MAX_HISTORY_PAGES} pages; older ones not collected")

        def waiver_claims() -> None:
            rows, capped = self._paged("FAB offers", "transactions?transactionsfilter=faab",
                                       parsers.parse_waiver_claims, season)
            snapshot.waiver_claims = rows
            if capped:
                warnings.append(f"FAB offers capped at {MAX_HISTORY_PAGES} pages; older ones not collected")

        def draft() -> None:
            names = {t.name: t.team_key for t in snapshot.teams}
            html = self.fetch("draft results", "draftresults")
            snapshot.draft_picks = self._parse("draft results", parsers.parse_draft_results, html, names)

        attempt("previous weeks' matchups", past_matchups)
        attempt("transactions", transactions)
        attempt("FAB offers", waiver_claims)
        attempt("draft results", draft)
        return warnings

    def _paged(self, name: str, path: str, parse: Callable, season: Optional[int]) -> Tuple[list, bool]:
        """Follow Yahoo's "Next 25" pager; returns (rows, hit_page_cap)."""
        rows: list = []
        for page_no in range(MAX_HISTORY_PAGES):
            offset = page_no * PAGE_SIZE
            label = f"{name} @{offset}"
            html = self.fetch(label, f"{path}&count={offset}")
            page_rows, has_next = self._parse(label, parse, html, self.league_id, season)
            rows.extend(page_rows)
            if not has_next:
                return rows, False
            if not page_rows:
                raise ExtractionError(f"{label}: empty page that still links to a next page")
        return rows, True

    def _scan_player_list(self, group: str, view: str, limit: int):
        rows: List[tuple] = []
        seen: set = set()
        pages, offset, reached_end = 0, 0, False
        while len(rows) < limit:
            path = (
                f"players?status=A&pos={group}&cut_type=9&stat1={view}"
                f"&myteam=0&sort=PTS&sdir=1&count={offset}"
            )
            html = self.fetch(f"players {group} {view} @{offset}", path)
            page = self._parse(f"players {group} {view} @{offset}", parsers.parse_player_list, html)
            pages += 1
            if not page.rows:
                if offset == 0:
                    raise ExtractionError(f"players {group} {view}: first page has no players")
                if page.has_next:
                    raise ExtractionError(f"players {group} {view}: empty page at offset {offset}")
                reached_end = True
                break
            for player, value in page.rows:
                # Lists can shift between page loads (adds/drops); skip repeats.
                if player.player_id not in seen:
                    seen.add(player.player_id)
                    rows.append((player, value))
            if not page.has_next:
                reached_end = True
                break
            offset += PAGE_SIZE
        return rows[:limit], PlayerScan(
            position_group=group, view=view, pages=pages, players=min(len(rows), limit), reached_end=reached_end
        )


def validate_snapshot(snapshot: LeagueSnapshot) -> tuple[List[str], List[str]]:
    """Return (errors, warnings). Errors mean the snapshot must not be used."""
    errors: List[str] = []
    warnings: List[str] = []
    team_keys = [t.team_key for t in snapshot.teams]
    known = set(team_keys)

    if len(team_keys) < 2:
        errors.append(f"only {len(team_keys)} team(s) found")
    if len(known) != len(team_keys):
        errors.append("duplicate team keys")
    if not snapshot.league.name:
        errors.append("league name missing")
    week = snapshot.league.current_week
    if week is None or not 1 <= week <= 18:
        errors.append(f"current week not detected (got {week!r})")
    if sum(t.is_mine for t in snapshot.teams) != 1:
        errors.append("could not identify exactly one team as yours")

    standing_keys = {s.team_key for s in snapshot.standings}
    if standing_keys - known:
        errors.append(f"standings reference unknown teams: {sorted(standing_keys - known)}")
    if known - standing_keys:
        errors.append(f"teams missing from standings: {sorted(known - standing_keys)}")

    for team in snapshot.teams:
        if not team.managers:
            warnings.append(f"{team.team_key}: no manager details (managers page)")

    roster_by_team = {r.team_key: r for r in snapshot.rosters}
    if set(roster_by_team) != known:
        errors.append(f"rosters missing for: {sorted(known - set(roster_by_team))}")
    owner = {}
    for key, roster in roster_by_team.items():
        if not roster.players:
            errors.append(f"{key}: roster is empty")
        for entry in roster.players:
            if entry.player_key in owner:
                errors.append(f"{entry.player_key} on two rosters ({owner[entry.player_key]}, {key})")
            owner[entry.player_key] = key
        _, overfilled = open_slots(snapshot.settings.roster_positions, roster.players)
        if overfilled:
            # Yahoo allows temporary over-limit rosters (e.g. right after a waiver claim).
            warnings.append(f"{key}: more players in slots than the league allows: {overfilled}")

    if snapshot.matchups:
        in_matchups = [key for m in snapshot.matchups for key in m.team_keys]
        if set(in_matchups) - known:
            errors.append(f"matchups reference unknown teams: {sorted(set(in_matchups) - known)}")
        if len(in_matchups) != len(set(in_matchups)):
            errors.append("a team appears in more than one current-week matchup")
        if {m.week for m in snapshot.matchups} != {snapshot.league.current_week}:
            errors.append("matchup week does not match current week")
        unmatched = known - set(in_matchups)
        if unmatched:
            warnings.append(f"teams without a current-week matchup (bye?): {sorted(unmatched)}")

    rostered_and_available = sorted(
        p.name for p in snapshot.available_players if p.player_key in owner
    )
    if rostered_and_available:
        # A transaction during the sync can cause this; keep the data but flag it.
        warnings.append(f"players listed as both rostered and available: {rostered_and_available}")
    capped = sorted({f"{s.position_group} top {s.players}" for s in snapshot.player_scans if not s.reached_end})
    if capped:
        # Intentional depth limit (see player_scans); stated so nobody mistakes it for the full pool.
        warnings.append(f"available players capped per view at depth limit: {', '.join(capped)}")

    # History checks are warnings only: history never blocks current-state data.
    per_week: Dict[int, int] = {}
    for m in snapshot.matchup_history:
        per_week[m.week] = per_week.get(m.week, 0) + 1
        if set(m.team_keys) - known:
            warnings.append(f"week {m.week} matchup has unknown teams: {sorted(set(m.team_keys) - known)}")
    for week_no, count in sorted(per_week.items()):
        if count != len(known) // 2:
            warnings.append(f"week {week_no}: {count} matchups, expected {len(known) // 2}")
    tx_teams = {t.team_key for t in snapshot.transactions} | {c.awarded_team_key for c in snapshot.waiver_claims}
    if tx_teams - known - {None}:
        warnings.append(f"transactions reference unknown teams: {sorted(tx_teams - known - {None})}")
    if snapshot.draft_picks:
        unmatched = sorted({p.team_name for p in snapshot.draft_picks if p.team_key is None})
        if unmatched:
            warnings.append(f"draft picks by unknown team names (renamed?): {unmatched}")
        rounds = max(p.round for p in snapshot.draft_picks)
        if len(snapshot.draft_picks) != rounds * len(known):
            warnings.append(f"draft has {len(snapshot.draft_picks)} picks, expected {rounds} x {len(known)}")

    if snapshot.settings.max_teams and snapshot.settings.max_teams != len(team_keys):
        warnings.append(
            f"settings Max Teams is {snapshot.settings.max_teams} but league has {len(team_keys)} teams"
        )
    return errors, warnings
