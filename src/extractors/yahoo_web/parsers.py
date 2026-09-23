"""Pure parsers: Yahoo Fantasy web page HTML → normalized league models.

No browser or network access here, so every parser can be tested offline against
saved fixture HTML. All Yahoo page-structure knowledge (ids, classes, column labels)
lives in this module; when Yahoo changes its markup, fix it here.

Parsers raise ``ParseError`` when a page lacks the structure they depend on, instead
of returning partial data silently. Optional fields that are absent become ``None``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from src.models.league_data import (
    AvailablePlayer,
    LeagueSettings,
    Matchup,
    MatchupSide,
    PlayoffSettings,
    RosterEntry,
    ScoringRule,
    Standing,
    Team,
    TeamRoster,
    player_key,
    team_key,
)

_EMPTY = {"", "-", "–", "—"}


class ParseError(ValueError):
    """A page did not have the structure a parser requires."""


# --------------------------------------------------------------------------- helpers


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


_ICON_GLYPHS = re.compile("[%s-%s]" % (chr(0xE000), chr(0xF8FF)))  # Yahoo icon-font glyphs (weather, actions)


def _text(el: Optional[Tag]) -> str:
    if el is None:
        return ""
    return " ".join(_ICON_GLYPHS.sub("", el.get_text(" ", strip=True)).split())


def _number(value: str) -> Optional[float]:
    value = value.strip().replace(",", "").replace("$", "").rstrip("%")
    if value in _EMPTY:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int(value: str) -> Optional[int]:
    number = _number(value)
    return int(number) if number is not None else None


def _yes_no(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered.startswith("yes"):
        return True
    if lowered.startswith("no"):
        return False
    return None


def _team_id_from_href(href: str, league_id: str) -> Optional[str]:
    match = re.search(rf"/f1/{re.escape(str(league_id))}/(\d+)/?$", urlsplit(href or "").path)
    return match.group(1) if match else None


def _header_labels(table: Tag) -> List[str]:
    """Column labels of the last header row, expanded by colspan to match body cells."""
    rows = table.select("thead tr")
    if not rows:
        return []
    labels: List[str] = []
    for cell in rows[-1].find_all(["th", "td"]):
        span = int(cell.get("colspan") or 1)
        labels.extend([_text(cell)] * span)
    return labels


def _column_index(labels: List[str], *names: str) -> Optional[int]:
    wanted = {n.lower() for n in names}
    for i, label in enumerate(labels):
        if label.lower() in wanted:
            return i
    return None


def _cell(cells: List[Tag], index: Optional[int]) -> str:
    if index is None or index >= len(cells):
        return ""
    return _text(cells[index])


# ------------------------------------------------------------------------ league home


@dataclass
class LeagueHome:
    name: Optional[str]
    season: Optional[int]
    current_week: Optional[int]
    my_team_id: Optional[str]
    standings: List[Tuple[str, str, Standing]]  # (team_id, team_name, standing)


def parse_league_home(html: str, league_id: str) -> LeagueHome:
    """League home page (/f1/<league_id>): standings, current week, season, my team."""
    soup = _soup(html)
    table = soup.select_one("#standingstable")
    if table is None:
        raise ParseError("League home page has no #standingstable")

    labels = _header_labels(table)
    i_rank = _column_index(labels, "Rank")
    i_wlt = _column_index(labels, "W-L-T")
    i_pf = _column_index(labels, "PF")
    i_pa = _column_index(labels, "PA")
    i_streak = _column_index(labels, "Streak")

    standings: List[Tuple[str, str, Standing]] = []
    my_team_id: Optional[str] = None
    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        link = next(
            (a for a in row.select("a[href]") if _text(a) and _team_id_from_href(a["href"], league_id)),
            None,
        )
        if link is None:
            continue
        team_id = _team_id_from_href(link["href"], league_id)
        wins, losses, ties = _parse_wlt(_cell(cells, i_wlt))
        standings.append(
            (
                team_id,
                _text(link),
                Standing(
                    team_key=team_key(league_id, team_id),
                    rank=_int(_cell(cells, i_rank)),
                    wins=wins,
                    losses=losses,
                    ties=ties,
                    points_for=_number(_cell(cells, i_pf)),
                    points_against=_number(_cell(cells, i_pa)),
                    streak=_cell(cells, i_streak) or None,
                ),
            )
        )
        if "Selected" in (row.get("class") or []):
            my_team_id = team_id
    if not standings:
        raise ParseError("#standingstable has no team rows")

    return LeagueHome(
        name=_league_name_from_title(soup),
        season=_selected_season(soup),
        current_week=_current_week(soup),
        my_team_id=my_team_id,
        standings=standings,
    )


def _parse_wlt(value: str) -> Tuple[int, int, int]:
    parts = [p for p in re.split(r"\s*-\s*", value.strip()) if p]
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ParseError(f"Unrecognized W-L-T value: {value!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def _league_name_from_title(soup: BeautifulSoup) -> Optional[str]:
    name = _text(soup.title).split(" | ")[0].strip()
    return name or None


def _selected_season(soup: BeautifulSoup) -> Optional[int]:
    select = soup.select_one("#seasonspec")
    if select is None:
        return None
    option = select.select_one("option[selected]") or select.select_one("option")
    match = re.search(r"(\d{4})", _text(option))
    return int(match.group(1)) if match else None


def _current_week(soup: BeautifulSoup) -> Optional[int]:
    title = soup.select_one("#matchup_selectlist_nav .flyout-title")
    match = re.search(r"Week\s+(\d+)", _text(title))
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------- settings


def parse_settings(html: str) -> LeagueSettings:
    """Settings page (/f1/<league_id>/settings): league rules and scoring."""
    soup = _soup(html)
    table = soup.select_one("#settings-table")
    if table is None:
        raise ParseError("Settings page has no #settings-table")

    raw: Dict[str, str] = {}
    for row in table.select("tr"):
        cells = row.find_all("td")
        if len(cells) >= 2:
            label = _text(cells[0]).rstrip(":").strip()
            if label:
                raw[label] = _text(cells[1])
    if not raw:
        raise ParseError("#settings-table has no setting rows")

    waiver_type = raw.get("Waiver Type")
    settings = LeagueSettings(
        raw=raw,
        max_teams=_int(raw.get("Max Teams", "")),
        scoring_type=raw.get("Scoring Type"),
        draft_type=raw.get("Draft Type"),
        draft_time=raw.get("Draft Time"),
        start_scoring_week=_int(re.sub(r"\D", "", raw.get("Start Scoring on", ""))),
        roster_positions=[p.strip() for p in raw.get("Roster Positions", "").split(",") if p.strip()],
        waiver_type=waiver_type,
        waiver_time=raw.get("Waiver Time"),
        uses_faab=bool(re.search(r"\bFAA?B\b", waiver_type)) if waiver_type else None,
        trade_end_date=raw.get("Trade End Date"),
        trade_review=raw.get("Trade Review"),
        max_trades_season=raw.get("Max Trades for Entire Season"),
        max_acquisitions_season=raw.get("Max Acquisitions for Entire Season"),
        max_acquisitions_week=raw.get("Max Acquisitions per Week"),
        divisions=_yes_no(raw.get("Divisions")),
        fractional_points=_yes_no(raw.get("Fractional Points")),
        negative_points=_yes_no(raw.get("Negative Points")),
        playoffs=_parse_playoffs(raw),
        scoring=_parse_scoring(soup),
    )
    if not settings.roster_positions:
        raise ParseError("Settings page has no Roster Positions")
    return settings


def _parse_playoffs(raw: Dict[str, str]) -> PlayoffSettings:
    text = raw.get("Playoffs")
    playoffs = PlayoffSettings(
        raw=text,
        tie_breaker=raw.get("Playoff Tie-Breaker"),
        reseeding=_yes_no(raw.get("Playoff Reseeding")),
    )
    if text:
        teams = re.search(r"(\d+)\s+teams?", text)
        playoffs.num_teams = int(teams.group(1)) if teams else None
        weeks = re.search(r"Weeks?\s+([\d,\sand]+)", text)
        if weeks:
            playoffs.weeks = [int(w) for w in re.findall(r"\d+", weeks.group(1))]
    return playoffs


def _parse_scoring(soup: BeautifulSoup) -> List[ScoringRule]:
    table = soup.select_one("#settings-stat-mod-table")
    if table is None:
        return []
    rules: List[ScoringRule] = []
    category = ""
    for row in table.select("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        # Hidden mobile-only badge ("Yahoo Default") sits inside the stat cell.
        for badge in cells[0].select("div"):
            badge.decompose()
        first, value = _text(cells[0]), _text(cells[1])
        if value == "League Value":  # category header row
            category = first
            continue
        default = _text(cells[2]) if len(cells) > 2 else ""
        rule = ScoringRule(category=category, stat=first, raw_value=value, yahoo_default=default or None)
        per_yards = re.match(r"([\d.]+)\s+yards?\s+per\s+point", value, re.I)
        if per_yards:
            rule.yards_per_point = float(per_yards.group(1))
        else:
            rule.points = _number(value)
        rules.append(rule)
    return rules


# --------------------------------------------------------------------------- managers


def parse_managers(html: str, league_id: str) -> List[Team]:
    """Managers page (/f1/<league_id>/teams): managers, FAAB, waiver priority, activity."""
    soup = _soup(html)
    table = next(
        (t for t in soup.select("table") if _column_index(_header_labels(t) or _first_row_labels(t), "Manager") is not None),
        None,
    )
    if table is None:
        raise ParseError("Managers page has no table with a Manager column")

    labels = _header_labels(table) or _first_row_labels(table)
    i_name = _column_index(labels, "Team Name", "Team")
    i_manager = _column_index(labels, "Manager")
    i_budget = _column_index(labels, "Waiver Budget", "FAAB")
    i_priority = _column_index(labels, "Waiver Priority", "Waiver")
    i_moves = _column_index(labels, "Moves")
    i_trades = _column_index(labels, "Trades")
    i_activity = _column_index(labels, "Last League Activity")

    teams: List[Team] = []
    for row in table.select("tr"):
        cells = row.find_all("td")
        if not cells or i_name is None or i_name >= len(cells):
            continue
        link = next(
            (a for a in cells[i_name].select("a[href]") if _text(a) and _team_id_from_href(a["href"], league_id)),
            None,
        )
        if link is None:
            continue
        team_id = _team_id_from_href(link["href"], league_id)
        manager_cell = cells[i_manager] if i_manager is not None and i_manager < len(cells) else None
        managers = [_text(a) for a in manager_cell.select("a")] if manager_cell is not None else []
        if manager_cell is not None and not managers and _text(manager_cell):
            managers = [_text(manager_cell)]
        teams.append(
            Team(
                team_key=team_key(league_id, team_id),
                team_id=team_id,
                name=_text(link),
                managers=[m for m in managers if m],
                is_commissioner="commissioner" in _text(manager_cell).lower(),
                faab_remaining=_number(_cell(cells, i_budget)),
                waiver_priority=_int(_cell(cells, i_priority)),
                moves=_moves(_cell(cells, i_moves)),
                trades=_moves(_cell(cells, i_trades)),
                last_activity=_cell(cells, i_activity) or None,
            )
        )
    if not teams:
        raise ParseError("Managers table has no team rows")
    return teams


def _first_row_labels(table: Tag) -> List[str]:
    row = table.select_one("tr")
    return [_text(c) for c in row.find_all(["th", "td"])] if row else []


def _moves(value: str) -> Optional[int]:
    # Yahoo shows "-" for a team with no moves yet.
    return 0 if value.strip() in _EMPTY else _int(value)


# ----------------------------------------------------------------------------- roster

_ROSTER_TABLE_IDS = ("statTable0", "statTable1", "statTable2")
_PLAYER_COLUMN_LABELS = ("Offense", "Kickers", "Defense/Special Teams", "Defensive Players", "Player")


def parse_team_roster(html: str, league_id: str, team_id: str, week: Optional[int] = None) -> TeamRoster:
    """Team page (/f1/<league_id>/<team_id>): every rostered player and slot."""
    soup = _soup(html)
    tables = [t for t in (soup.select_one(f"#{tid}") for tid in _ROSTER_TABLE_IDS) if t is not None]
    if not tables:
        raise ParseError(f"Team page {team_id} has no roster tables (#statTable0..2)")

    key = team_key(league_id, team_id)
    players: List[RosterEntry] = []
    seen: set = set()
    for table in tables:
        labels = _header_labels(table)
        idx = {
            "player": _column_index(labels, *_PLAYER_COLUMN_LABELS),
            "bye": _column_index(labels, "Bye"),
            "pts": _column_index(labels, "Fan Pts"),
            "proj": _column_index(labels, "Proj Pts"),
            "start": _column_index(labels, "% Start"),
            "ros": _column_index(labels, "% Ros"),
        }
        for row in table.select("tbody tr"):
            cells = row.find_all("td")
            slot_el = row.select_one(".pos-label")
            slot = (slot_el.get("data-pos") or _text(slot_el)) if slot_el else _cell(cells, 0)
            player = _player_cell(row)
            # Rows without a player are "(Empty)" slots; Yahoo shows them inconsistently,
            # so open slots are derived from league settings instead (open_slots()).
            if not slot or player is None or player.player_id in seen:
                continue
            seen.add(player.player_id)
            players.append(_roster_entry(row, cells, idx, key, player, slot))

    return TeamRoster(team_key=key, week=week, players=players)


@dataclass
class _PlayerCell:
    """Identity and status from Yahoo's standard player cell (rosters, player lists)."""

    player_id: str
    name: str
    nfl_team: Optional[str]
    positions: List[str]
    status: Optional[str]
    status_full: Optional[str]
    game: Optional[str]


def _player_cell(row: Tag) -> Optional[_PlayerCell]:
    name_link = row.select_one("a.name[data-ys-playerid]")
    if name_link is None:
        return None
    nfl_team, positions = None, []
    team_pos = row.select_one(".ysf-player-name .D-b .Fz-xxs") or row.select_one(".ysf-player-name .Fz-xxs")
    match = re.match(r"\s*([A-Za-z]{2,4})\s*-\s*([A-Z/,\s]+)", _text(team_pos))
    if match:
        nfl_team = match.group(1).upper()
        positions = [p.strip() for p in match.group(2).split(",") if p.strip()]
    status_el = row.select_one(".ysf-player-status [title]")
    return _PlayerCell(
        player_id=name_link["data-ys-playerid"],
        name=name_link.get("title") or _text(name_link),
        nfl_team=nfl_team,
        positions=positions,
        status=_text(status_el) or None,
        status_full=status_el.get("title") if status_el else None,
        game=_text(row.select_one(".ysf-game-status")) or None,
    )


def _roster_entry(
    row: Tag, cells: List[Tag], idx: Dict[str, Optional[int]], key: str, player: _PlayerCell, slot: str
) -> RosterEntry:
    return RosterEntry(
        team_key=key,
        player_key=player_key(player.player_id),
        player_id=player.player_id,
        name=player.name,
        slot=slot,
        nfl_team=player.nfl_team,
        positions=player.positions,
        status=player.status,
        status_full=player.status_full,
        bye_week=_int(_cell(cells, idx["bye"])),
        fantasy_points=_number(_cell(cells, idx["pts"])),
        projected_points=_number(_cell(cells, idx["proj"])),
        percent_started=_number(_cell(cells, idx["start"])),
        percent_rostered=_number(_cell(cells, idx["ros"])),
        game=player.game,
    )


# --------------------------------------------------------------------------- matchups


def parse_matchups(html: str, league_id: str) -> List[Matchup]:
    """Current-week matchups from the league home page's matchup module (#matchupweek)."""
    soup = _soup(html)
    module = soup.select_one("#matchupweek")
    if module is None:
        raise ParseError("League home page has no #matchupweek module")
    section = module.select_one(".Submod.Selected") or module
    header = _text(section.select_one(".Bg-shade") or section)
    week_match = re.search(r"Week\s+(\d+)\s+Matchups", header)
    if not week_match:
        raise ParseError("Matchup module has no 'Week N Matchups' header")
    week = int(week_match.group(1))
    status = _text(section.select_one(".Bg-shade .Ta-end")) or None

    matchups: List[Matchup] = []
    for item in section.select("li[data-target*='/matchup?']"):
        sides: List[MatchupSide] = []
        for half in item.select(".Grid-u-6-13"):
            link = next(
                (a for a in half.select("a[href]") if _text(a) and _team_id_from_href(a["href"], league_id)),
                None,
            )
            if link is None:
                continue
            score = half.select_one("div.Fz-lg")
            projected = score.find_next_sibling("div") if score is not None else None
            sides.append(
                MatchupSide(
                    team_key=team_key(league_id, _team_id_from_href(link["href"], league_id)),
                    points=_number(_text(score)),
                    projected_points=_number(_text(projected)),
                )
            )
        if len(sides) != 2:
            raise ParseError(f"Week {week} matchup row has {len(sides)} teams, expected 2")
        matchups.append(Matchup(week=week, status=status, teams=sides))
    if not matchups:
        raise ParseError(f"Week {week} matchup module has no matchups")
    return matchups


# ------------------------------------------------------------------------ player list


@dataclass
class PlayerListPage:
    rows: List[Tuple[AvailablePlayer, Optional[float]]]  # (player, value of the view's "Fan Pts")
    has_next: bool


def parse_player_list(html: str) -> PlayerListPage:
    """One page (25 rows max) of the Players page (/f1/<league_id>/players?...).

    The "Fan Pts" column means different things per stat view (week projection,
    rest-of-season projection, season total), so it is returned separately.
    """
    soup = _soup(html)
    table = next(
        (t for t in soup.select("table") if _column_index(_header_labels(t), "Roster Status") is not None),
        None,
    )
    if table is None:
        raise ParseError("Players page has no player table with a Roster Status column")
    labels = _header_labels(table)
    i_status = _column_index(labels, "Roster Status")
    i_gp = _column_index(labels, "GP*", "GP")
    i_bye = _column_index(labels, "Bye")
    i_pts = _column_index(labels, "Fan Pts")
    i_pre = _column_index(labels, "Pre-Season")
    i_actual = _column_index(labels, "Actual")
    i_ros = _column_index(labels, "% Ros")

    rows: List[Tuple[AvailablePlayer, Optional[float]]] = []
    for row in table.select("tbody tr"):
        player = _player_cell(row)
        if player is None:
            continue
        cells = row.find_all("td")
        availability, waiver_until = _availability(_cell(cells, i_status))
        rows.append(
            (
                AvailablePlayer(
                    player_key=player_key(player.player_id),
                    player_id=player.player_id,
                    name=player.name,
                    availability=availability,
                    waiver_until=waiver_until,
                    nfl_team=player.nfl_team,
                    positions=player.positions,
                    status=player.status,
                    status_full=player.status_full,
                    bye_week=_int(_cell(cells, i_bye)),
                    games_played=_int(_cell(cells, i_gp)),
                    percent_rostered=_number(_cell(cells, i_ros)),
                    preseason_rank=_int(_cell(cells, i_pre)),
                    current_rank=_int(_cell(cells, i_actual)),
                    game=player.game,
                ),
                _number(_cell(cells, i_pts)),
            )
        )

    paging = soup.select_one(".pagingnavlist")
    has_next = bool(paging) and any(_text(a).lower().startswith("next") for a in paging.select("a[href]"))
    return PlayerListPage(rows=rows, has_next=has_next)


def _availability(value: str) -> Tuple[str, Optional[str]]:
    """'FA' → free agent; 'W (Sep 23)' → on waivers until Sep 23."""
    value = value.strip()
    if value.upper() == "FA":
        return "free_agent", None
    waiver = re.match(r"W\s*(?:\((.+)\))?$", value)
    if waiver:
        return "waivers", waiver.group(1)
    return value or "unknown", None


# ---------------------------------------------------------------------------- merging


def open_slots(roster_positions: List[str], players: List[RosterEntry]) -> Tuple[Dict[str, int], Dict[str, int]]:
    """(open, overfilled) slot counts: league roster positions vs. slots players occupy."""
    capacity = Counter(roster_positions)
    used = Counter(p.slot for p in players)
    open_ = {slot: n - used[slot] for slot, n in capacity.items() if n > used[slot]}
    over = {slot: n - capacity[slot] for slot, n in used.items() if n > capacity[slot]}
    return open_, over


def merge_teams(home: LeagueHome, managers: List[Team]) -> List[Team]:
    """Combine standings-page team list with managers-page details.

    Standings names are authoritative (the nav link for your own team reads "My Team").
    """
    by_id = {t.team_id: t for t in managers}
    teams: List[Team] = []
    for team_id, name, standing in home.standings:
        base = by_id.get(team_id) or Team(team_key=standing.team_key, team_id=team_id, name=name)
        teams.append(replace(base, name=name, is_mine=team_id == home.my_team_id))
    return teams
