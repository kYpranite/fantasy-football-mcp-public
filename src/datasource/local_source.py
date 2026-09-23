"""Read-only league queries over the local SQLite store, shaped for MCP tools.

Every method returns plain JSON-serializable dicts/lists. Nothing here touches Yahoo:
data comes from the latest committed sync (``utils/sync_yahoo_league.py``). Responses
carry ``synced_at`` so the model can tell how fresh the data is.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.models.league_data import AvailablePlayer, LeagueSnapshot, RosterEntry
from src.storage.db import connect, default_db_path
from src.storage.repository import LeagueStore

STALE_AFTER_HOURS = 24
FLEX_POSITIONS = {"W/R/T": {"WR", "RB", "TE"}, "W/R": {"WR", "RB"}, "W/T": {"WR", "TE"},
                  "Q/W/R/T": {"QB", "WR", "RB", "TE"}}
SORTS = {
    "rank": "projected_rest_of_season",  # legacy name: best rest-of-season value first
    "projected_rest_of_season": "projected_rest_of_season",
    "projected_week": "projected_week",
    "points": "season_points",
    "season_points": "season_points",
    "owned": "percent_rostered",
    "percent_rostered": "percent_rostered",
    "trending": "percent_rostered",  # no add/drop trend data yet; see note in response
}


class NoLocalData(LookupError):
    """No synced data for the requested league (or the database is empty)."""


def normalize_league_key(value: Optional[str]) -> Optional[str]:
    """Accept 'nfl.l.269337', '461.l.269337', or '269337' → 'nfl.l.269337'."""
    if not value:
        return None
    value = str(value).strip()
    match = re.search(r"(?:^|\.)l\.(\d+)", value)
    if match:
        return f"nfl.l.{match.group(1)}"
    return f"nfl.l.{value}" if value.isdigit() else value


def normalize_team_key(value: Optional[str], league_key: str) -> Optional[str]:
    """Accept 'nfl.l.X.t.4', '461.l.X.t.4', or '4' → 'nfl.l.X.t.4'."""
    if not value:
        return None
    value = str(value).strip()
    match = re.search(r"\.t\.(\d+)$", value)
    team_id = match.group(1) if match else (value if value.isdigit() else None)
    return f"{league_key}.t.{team_id}" if team_id else value


def _fold(text: str) -> str:
    """Lowercase, accent- and punctuation-insensitive form for name search."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", text.lower())


def _opponent(game: Optional[str]) -> Optional[str]:
    """'Sun 1:00 pm @ Jax' → 'JAX'."""
    match = re.search(r"(?:vs|@)\s*([A-Za-z]{2,4})\s*$", game or "")
    return match.group(1).upper() if match else None


class LocalLeagueSource:
    def __init__(self, db_path: Optional[Union[str, Path]] = None, store: Optional[LeagueStore] = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self._store = store
        self._cache: Dict[str, Tuple[int, LeagueSnapshot]] = {}

    # ----------------------------------------------------------------- plumbing

    @property
    def store(self) -> LeagueStore:
        if self._store is None:
            if not self.db_path.exists():
                raise NoLocalData(
                    f"No league database at {self.db_path}. Run: "
                    "python utils/sync_yahoo_league.py --league-id <id>"
                )
            self._store = LeagueStore(connect(self.db_path))
        return self._store

    def resolve_league(self, league_key: Optional[str]) -> str:
        key = normalize_league_key(league_key)
        if key:
            return key
        keys = self.store.league_keys()
        if len(keys) == 1:
            return keys[0]
        raise NoLocalData("league_key is required" if keys else "No synced leagues yet")

    def snapshot(self, league_key: Optional[str]) -> LeagueSnapshot:
        key = self.resolve_league(league_key)
        run_id = self.store.latest_run_id(key)
        if run_id is None:
            raise NoLocalData(
                f"No synced data for {key}. Run: python utils/sync_yahoo_league.py --league-id "
                f"{key.rsplit('.', 1)[-1]}"
            )
        cached = self._cache.get(key)
        if cached is None or cached[0] != run_id:
            cached = (run_id, self.store.load_snapshot(key, run_id))
            self._cache[key] = cached
        return cached[1]

    def _meta(self, s: LeagueSnapshot) -> Dict[str, Any]:
        return {"league_key": s.league.league_key, "synced_at": s.captured_at, "data_source": "local"}

    @staticmethod
    def _names(s: LeagueSnapshot) -> Dict[str, str]:
        return {t.team_key: t.name for t in s.teams}

    def _my_team_key(self, s: LeagueSnapshot) -> Optional[str]:
        mine = s.my_team
        return mine.team_key if mine else None

    def _team_key(self, s: LeagueSnapshot, team_key: Optional[str], default_mine: bool = True) -> str:
        key = normalize_team_key(team_key, s.league.league_key) if team_key else None
        if key is None and default_mine:
            key = self._my_team_key(s)
        if key is None or s.team(key) is None:
            raise NoLocalData(f"Unknown team {team_key!r}; known teams: {[t.team_key for t in s.teams]}")
        return key

    @staticmethod
    def _player(p: Union[RosterEntry, AvailablePlayer]) -> Dict[str, Any]:
        data = asdict(p)
        data.pop("team_key", None)
        data.pop("player_id", None)
        return data

    # ---------------------------------------------------------------- league level

    def leagues(self) -> Dict[str, Any]:
        leagues = []
        for key in self.store.league_keys():
            s = self.snapshot(key)
            leagues.append({
                "key": key, "name": s.league.name, "season": s.league.season, "teams": s.league.num_teams,
                "current_week": s.league.current_week, "scoring": s.settings.scoring_type,
                "synced_at": s.captured_at,
            })
        return {"total_leagues": len(leagues), "leagues": leagues, "data_source": "local"}

    def league_info(self, league_key: Optional[str]) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        standings = {x.team_key: x for x in s.standings}
        mine = s.my_team
        my_standing = standings.get(mine.team_key) if mine else None
        settings = s.settings
        return {
            **self._meta(s),
            "league": s.league.name,
            "key": s.league.league_key,
            "season": s.league.season,
            "teams": s.league.num_teams,
            "current_week": s.league.current_week,
            "scoring_type": settings.scoring_type,
            "status": "active",
            "roster_positions": settings.roster_positions,
            "waivers": {"type": settings.waiver_type, "period": settings.waiver_time, "uses_faab": settings.uses_faab},
            "trade_deadline": settings.trade_end_date,
            "playoffs": asdict(settings.playoffs),
            "your_team": {
                "name": mine.name if mine else "Not found",
                "key": mine.team_key if mine else None,
                "rank": my_standing.rank if my_standing else None,
                "record": f"{my_standing.wins}-{my_standing.losses}-{my_standing.ties}" if my_standing else None,
                "faab_remaining": mine.faab_remaining if mine else None,
                "waiver_priority": mine.waiver_priority if mine else None,
            },
        }

    def league_settings(self, league_key: Optional[str]) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        settings = asdict(s.settings)
        raw = settings.pop("raw")
        return {**self._meta(s), "league": s.league.name, **settings, "all_settings": raw}

    def teams(self, league_key: Optional[str]) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        standings = {x.team_key: x for x in s.standings}
        rosters = {r.team_key: r for r in s.rosters}
        teams = []
        for t in s.teams:
            row = asdict(t)
            st = standings.get(t.team_key)
            if st:
                row.update(rank=st.rank, record=f"{st.wins}-{st.losses}-{st.ties}",
                           points_for=st.points_for, points_against=st.points_against, streak=st.streak)
            roster = rosters.get(t.team_key)
            if roster:
                row.update(roster_size=len(roster.players), open_slots=roster.empty_slots)
            teams.append(row)
        return {**self._meta(s), "teams": teams, "total_teams": len(teams)}

    def standings(self, league_key: Optional[str]) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        teams = {t.team_key: t for t in s.teams}
        rows = []
        for st in sorted(s.standings, key=lambda x: x.rank or 99):
            team = teams.get(st.team_key)
            rows.append({
                "rank": st.rank, "team": team.name if team else st.team_key, "team_key": st.team_key,
                "wins": st.wins, "losses": st.losses, "ties": st.ties,
                "points_for": st.points_for, "points_against": st.points_against, "streak": st.streak,
                "faab_remaining": team.faab_remaining if team else None,
                "waiver_priority": team.waiver_priority if team else None,
                "is_mine": bool(team and team.is_mine),
            })
        return {**self._meta(s), "standings": rows, "playoff_teams": s.settings.playoffs.num_teams}

    # ---------------------------------------------------------------------- rosters

    def _roster(self, s: LeagueSnapshot, team_key: str):
        return next((r for r in s.rosters if r.team_key == team_key), None)

    def roster(self, league_key: Optional[str], team_key: Optional[str] = None) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        key = self._team_key(s, team_key)
        team = s.team(key)
        roster = self._roster(s, key)
        players = [self._player(p) for p in roster.players] if roster else []
        return {
            **self._meta(s),
            "status": "success",
            "team_key": key,
            "team_name": team.name,
            "is_mine": team.is_mine,
            "week": roster.week if roster else s.league.current_week,
            "starters": [p for p in players if p["slot"] not in ("BN", "IR")],
            "bench": [p for p in players if p["slot"] == "BN"],
            "injured_reserve": [p for p in players if p["slot"] == "IR"],
            "open_slots": roster.empty_slots if roster else {},
            "roster": players,
        }

    def legacy_roster(self, league_key: Optional[str], team_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """Roster in the dict shape the legacy enhancement / lineup optimizer consume."""
        s = self.snapshot(league_key)
        roster = self._roster(s, self._team_key(s, team_key))
        return [
            {
                "name": p.name, "player_key": p.player_key,
                "position": p.positions[0] if p.positions else p.slot,
                "selected_position": p.slot, "eligible_positions": p.positions,
                "team": p.nfl_team or "", "status": p.status or "OK", "bye": p.bye_week,
                "yahoo_projection": p.projected_points, "opponent": _opponent(p.game) or "",
                "percent_rostered": p.percent_rostered,
            }
            for p in (roster.players if roster else [])
        ]

    def all_rosters(self, league_key: Optional[str], position: Optional[str] = None) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        wanted = self._position_filter(position)
        teams = []
        for t in s.teams:
            roster = self._roster(s, t.team_key)
            players = [
                {"name": p.name, "player_key": p.player_key, "positions": p.positions, "slot": p.slot,
                 "nfl_team": p.nfl_team, "status": p.status, "bye_week": p.bye_week,
                 "projected_points": p.projected_points, "percent_rostered": p.percent_rostered}
                for p in (roster.players if roster else [])
                if wanted is None or wanted & set(p.positions)
            ]
            teams.append({"team_key": t.team_key, "team_name": t.name, "is_mine": t.is_mine, "players": players,
                          "open_slots": roster.empty_slots if roster else {}})
        return {**self._meta(s), "position_filter": position, "teams": teams}

    def compare_teams(self, league_key: Optional[str], team_key_a: str, team_key_b: str) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        sides = []
        for raw_key in (team_key_a, team_key_b):
            key = self._team_key(s, raw_key, default_mine=False)
            roster = self._roster(s, key)
            by_pos: Dict[str, List[Dict[str, Any]]] = {}
            for p in roster.players if roster else []:
                pos = p.positions[0] if p.positions else p.slot
                by_pos.setdefault(pos, []).append(
                    {"name": p.name, "player_key": p.player_key, "slot": p.slot, "status": p.status,
                     "projected_points": p.projected_points, "percent_rostered": p.percent_rostered})
            summary = {
                pos: {"count": len(ps),
                      "projected_points": round(sum(x["projected_points"] or 0 for x in ps), 2),
                      "starters": sum(1 for x in ps if x["slot"] not in ("BN", "IR"))}
                for pos, ps in by_pos.items()
            }
            standing = next((x for x in s.standings if x.team_key == key), None)
            sides.append({"team_key": key, "team_name": s.team(key).name,
                          "record": f"{standing.wins}-{standing.losses}-{standing.ties}" if standing else None,
                          "rank": standing.rank if standing else None,
                          "by_position": by_pos, "position_summary": summary})
        return {**self._meta(s), "team_a": sides[0], "team_b": sides[1]}

    # --------------------------------------------------------------------- matchups

    def matchups(self, league_key: Optional[str], week: Optional[int] = None) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        week = int(week) if week else s.league.current_week
        names = self._names(s)
        source = s.matchups if week == s.league.current_week else [m for m in s.matchup_history if m.week == week]
        rows = [{
            "week": m.week, "status": m.status, "winner_team_key": m.winner_team_key,
            "involves_me": self._my_team_key(s) in m.team_keys,
            "teams": [{"team_key": side.team_key, "team_name": names.get(side.team_key), "points": side.points,
                       "projected_points": side.projected_points} for side in m.teams],
        } for m in source]
        available = sorted({m.week for m in s.matchup_history} | {s.league.current_week})
        return {**self._meta(s), "week": week, "matchups": rows,
                **({} if rows else {"note": f"No matchups stored for week {week}; weeks available: {available}"})}

    def my_matchup(self, league_key: Optional[str], week: Optional[int] = None,
                   team_key: Optional[str] = None) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        me = self._team_key(s, team_key)
        data = self.matchups(s.league.league_key, week)
        mine = next((m for m in data["matchups"] if me in [t["team_key"] for t in m["teams"]]), None)
        result = {**self._meta(s), "team_key": me, "week": data["week"], "matchup": mine}
        if mine is None:
            result["note"] = data.get("note") or "No matchup found for this team and week (bye?)"
            return result
        opponent = next(t["team_key"] for t in mine["teams"] if t["team_key"] != me)
        result["opponent"] = {"team_key": opponent, "team_name": s.team(opponent).name if s.team(opponent) else None}
        if data["week"] == s.league.current_week:
            result["my_roster"] = self._starters(s, me)
            result["opponent_roster"] = self._starters(s, opponent)
        return result

    def _starters(self, s: LeagueSnapshot, team_key: str) -> List[Dict[str, Any]]:
        roster = self._roster(s, team_key)
        return [self._player(p) for p in (roster.players if roster else []) if p.slot not in ("BN", "IR")]

    # ------------------------------------------------------------- available players

    @staticmethod
    def _position_filter(position: Optional[str]) -> Optional[set]:
        if not position or position.lower() in ("all", "any"):
            return None
        pos = position.upper()
        if pos in ("O", "OFFENSE"):
            return {"QB", "RB", "WR", "TE"}
        return FLEX_POSITIONS.get(pos, {pos, "DEF"} if pos in ("D/ST", "DST") else {pos})

    def available_players(
        self, league_key: Optional[str], position: Optional[str] = None, sort: str = "rank",
        count: int = 25, availability: Optional[str] = None, include_injured: bool = True,
    ) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        wanted = self._position_filter(position)
        field = SORTS.get((sort or "rank").lower(), "projected_rest_of_season")
        players = [
            p for p in s.available_players
            if (wanted is None or wanted & set(p.positions))
            and (availability is None or p.availability == availability)
            and (include_injured or not p.status)
        ]
        players.sort(key=lambda p: (getattr(p, field) is None, -(getattr(p, field) or 0)))
        capped = [f"{x.position_group} top {x.players}" for x in s.player_scans if not x.reached_end]
        result = {
            **self._meta(s), "status": "success", "position": position or "all", "sort": field,
            "total_available_matching": len(players), "players": [self._player(p) for p in players[: max(1, count)]],
            "scan_depth": sorted(set(capped)) or "complete",
        }
        if (sort or "").lower() == "trending":
            result["note"] = "Add/drop trend data is not synced yet; sorted by % rostered instead."
        return result

    def legacy_waiver_players(self, league_key: Optional[str], position: str = "all", sort: str = "rank",
                              count: int = 30) -> List[Dict[str, Any]]:
        """Available players in the dict shape the legacy waiver-wire handler consumes."""
        data = self.available_players(league_key, position, sort, count)
        return [
            {
                "name": p["name"], "player_key": p["player_key"],
                "position": p["positions"][0] if p["positions"] else "", "eligible_positions": p["positions"],
                "team": p["nfl_team"] or "FA", "owned_pct": p["percent_rostered"] or 0, "weekly_change": None,
                "injury_status": p["status"] or "Healthy", "injury_detail": p["status_full"], "bye": p["bye_week"],
                "availability": p["availability"], "waiver_until": p["waiver_until"],
                "yahoo_projection": p["projected_week"], "projected_rest_of_season": p["projected_rest_of_season"],
                "season_points": p["season_points"], "opponent": _opponent(p["game"]) or "",
            }
            for p in data["players"]
        ]

    def search_players(self, league_key: Optional[str], query: str, limit: int = 10) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        needle = _fold(query)
        names = self._names(s)
        found: Dict[str, Dict[str, Any]] = {}
        for roster in s.rosters:
            for p in roster.players:
                if needle in _fold(p.name):
                    found[p.player_key] = {**self._player(p), "ownership": "rostered",
                                           "fantasy_team_key": roster.team_key,
                                           "fantasy_team": names.get(roster.team_key)}
        for p in s.available_players:
            if needle in _fold(p.name) and p.player_key not in found:
                found[p.player_key] = {**self._player(p), "ownership": p.availability}
        if len(found) < limit:  # players seen only in history (transactions/draft), outside scanned lists
            for row in self.store.conn.execute(
                "SELECT player_key, name, nfl_team, positions_json FROM players"
            ):
                if needle in _fold(row["name"]) and row["player_key"] not in found:
                    found[row["player_key"]] = {
                        "player_key": row["player_key"], "name": row["name"], "nfl_team": row["nfl_team"],
                        "ownership": "unknown (not on a roster; outside the synced available-player lists)",
                    }
        matches = list(found.values())[:limit]
        return {**self._meta(s), "query": query, "matches": matches, "total_matches": len(found)}

    def player_history(self, league_key: Optional[str], player_key: str) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        key = player_key if player_key.startswith("nfl.p.") else f"nfl.p.{player_key}"
        names = self._names(s)
        runs = self.store.player_roster_history(key)
        return {
            **self._meta(s), "player_key": key,
            "roster_history": [{**r, "team_name": names.get(r["team_key"])} for r in runs],
            "transactions": self.transactions(s.league.league_key, player=key, limit=100)["transactions"],
            "drafted": next((asdict(d) for d in s.draft_picks if d.player_key == key), None),
        }

    def _all_known_players(self, s: LeagueSnapshot) -> List[Dict[str, Any]]:
        """Every player the store knows, with ownership (rostered > available > history only)."""
        names = self._names(s)
        known: Dict[str, Dict[str, Any]] = {}
        for roster in s.rosters:
            for p in roster.players:
                known[p.player_key] = {**self._player(p), "ownership": "rostered",
                                       "fantasy_team_key": roster.team_key, "fantasy_team": names.get(roster.team_key)}
        for p in s.available_players:
            known.setdefault(p.player_key, {**self._player(p), "ownership": p.availability})
        for row in self.store.conn.execute("SELECT player_key, name, nfl_team, positions_json FROM players"):
            known.setdefault(row["player_key"], {
                "player_key": row["player_key"], "name": row["name"], "nfl_team": row["nfl_team"],
                "positions": json.loads(row["positions_json"]),
                "ownership": "unknown (not on a roster; outside the synced available-player lists)",
            })
        return list(known.values())

    def player_context(self, league_key: Optional[str], player: str) -> Dict[str, Any]:
        """Resolve one player in the synced league data (database access only)."""
        s = self.snapshot(league_key)
        query = (player or "").strip()
        key = query if query.startswith("nfl.p.") else (f"nfl.p.{query}" if query.isdigit() else None)
        if key is not None:
            matches = [m for m in self._all_known_players(s) if m["player_key"] == key]
        else:
            found = self.search_players(s.league.league_key, query, limit=50)["matches"]
            exact = [m for m in found if _fold(m["name"]) == _fold(query)]
            matches = exact or found
        if not matches:
            return {**self._meta(s), "status": "error", "error": f"No player matching {player!r} in synced league data"}
        if len(matches) > 1:
            return {**self._meta(s), "status": "ambiguous",
                    "message": "Several players match; call again with a player_key.",
                    "matches": [{k: m.get(k) for k in ("player_key", "name", "nfl_team", "positions", "ownership",
                                                       "fantasy_team")} for m in matches[:10]]}
        context = matches[0]
        return {**self._meta(s), "status": "success", "player_key": context["player_key"],
                "name": context["name"], "season": s.league.season, "league_context": context}

    @staticmethod
    def add_sleeper_details(result: Dict[str, Any], details_client=None) -> Dict[str, Any]:
        """Attach Sleeper data to a ``player_context`` result (network only; no database access,
        so it is safe to run in a worker thread)."""
        from src.datasource.player_details import PlayerDetailsError, SleeperPlayerDetails

        if result.get("status") != "success":
            return result
        context = result["league_context"]
        positions = context.get("positions") or []
        client = details_client or SleeperPlayerDetails()
        try:
            result["sleeper"] = client.details(
                context["player_key"].rsplit(".", 1)[-1], context["name"],
                positions[0] if positions else None, context.get("nfl_team"), result.pop("season"),
            )
        except PlayerDetailsError as exc:
            result["sleeper_error"] = str(exc)
        return result

    def player_details(self, league_key: Optional[str], player: str, details_client=None) -> Dict[str, Any]:
        """League context for one player plus Sleeper game logs, usage, injury, depth chart."""
        return self.add_sleeper_details(self.player_context(league_key, player), details_client)

    # ------------------------------------------------------------------------ history

    def transactions(self, league_key: Optional[str], team_key: Optional[str] = None,
                     type: Optional[str] = None, player: Optional[str] = None,
                     since: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        names = self._names(s)
        team = self._team_key(s, team_key, default_mine=False) if team_key else None
        wanted_player = _fold(player) if player and not player.startswith("nfl.p.") else None
        rows = []
        for t in s.transactions:
            if team and t.team_key != team:
                continue
            if type and type.lower() not in t.type:
                continue
            if since and (t.timestamp or "") < since:
                continue
            if player and not any(
                p.player_key == player or (wanted_player and wanted_player in _fold(p.name)) for p in t.players
            ):
                continue
            rows.append({**asdict(t), "team_name": names.get(t.team_key)})
        return {**self._meta(s), "total_matching": len(rows), "transactions": rows[: max(1, limit)]}

    def faab_bids(self, league_key: Optional[str], team_key: Optional[str] = None) -> Dict[str, Any]:
        """FAAB activity per manager.

        Waiver adds and FAAB spent come from the transaction history ("$N Waiver" adds), which
        includes every processed claim. Losing bids come from Yahoo's FAB Offers page, which only
        lists CONTESTED claims (at least one competing bid).
        """
        s = self.snapshot(league_key)
        names = self._names(s)
        team = self._team_key(s, team_key, default_mine=False) if team_key else None
        claims = [c for c in s.waiver_claims if team is None or any(b.team_key == team for b in c.bids)]
        managers: Dict[str, Dict[str, Any]] = {
            t.team_key: {"team_name": t.name, "faab_remaining": t.faab_remaining, "waiver_adds": 0,
                         "faab_spent": 0.0, "max_winning_bid": None, "losing_bids": 0, "max_losing_bid": None,
                         "waiver_add_history": [], "losing_bid_history": []}
            for t in s.teams
        }
        for t in s.transactions:
            m = managers.get(t.team_key)
            if m is None:
                continue
            for p in t.players:
                if p.action == "add" and p.faab_bid is not None:
                    m["waiver_adds"] += 1
                    m["faab_spent"] += p.faab_bid
                    m["max_winning_bid"] = max(m["max_winning_bid"] or 0, p.faab_bid)
                    m["waiver_add_history"].append({"player": p.name, "bid": p.faab_bid, "when": t.timestamp})
        for c in s.waiver_claims:
            for b in c.bids[1:]:  # bids[0] is the winner, already counted from transactions
                m = managers.get(b.team_key)
                if m is None:
                    continue
                m["losing_bids"] += 1
                if b.bid is not None:
                    m["max_losing_bid"] = max(m["max_losing_bid"] or 0, b.bid)
                m["losing_bid_history"].append({"player": c.name, "bid": b.bid, "reason": b.result,
                                                "won_by": names.get(c.awarded_team_key),
                                                "winning_bid": c.winning_bid, "when": c.timestamp})
        return {
            **self._meta(s),
            "waiver_rules": {"type": s.settings.waiver_type, "period": s.settings.waiver_time},
            "note": "waiver_adds/faab_spent include every processed waiver claim (from transactions). "
                    "contested_claims lists only claims with competing bids (Yahoo's FAB Offers page).",
            "contested_claims": [{**asdict(c), "awarded_team": names.get(c.awarded_team_key),
                                  "bids": [{**asdict(b), "team_name": names.get(b.team_key)} for b in c.bids]}
                                 for c in claims],
            "by_manager": managers if team is None else {team: managers[team]},
        }

    def draft_results(self, league_key: Optional[str], team_key: Optional[str] = None) -> Dict[str, Any]:
        s = self.snapshot(league_key)
        owner = {p.player_key: r.team_key for r in s.rosters for p in r.players}
        available = {p.player_key: p.availability for p in s.available_players}
        names = self._names(s)
        team = self._team_key(s, team_key, default_mine=False) if team_key else None
        picks = []
        for d in s.draft_picks:
            if team and d.team_key != team:
                continue
            now = owner.get(d.player_key)
            status = ("still on drafting team" if now == d.team_key else
                      f"now on {names.get(now, now)}" if now else
                      available.get(d.player_key, "not rostered"))
            picks.append({**asdict(d), "current_team_key": now, "current_status": status})
        return {**self._meta(s), "total_picks": len(picks), "picks": picks}

    # ------------------------------------------------------------------------ status

    def sync_status(self, league_key: Optional[str] = None) -> Dict[str, Any]:
        from src.datasource.sync_trigger import log_tail, read_lock

        running = read_lock()
        in_progress = (
            {"started_at_epoch": running["started_at"], "mode": running.get("mode")} if running else None
        )
        try:
            s = self.snapshot(league_key)
        except NoLocalData as exc:
            return {"status": "no_data", "message": str(exc), "data_source": "local",
                    "sync_in_progress": in_progress}
        age_hours = None
        try:
            captured = datetime.fromisoformat(s.captured_at)
            age_hours = round((datetime.now(timezone.utc) - captured).total_seconds() / 3600, 1)
        except (TypeError, ValueError):
            pass
        result = {
            **self._meta(s), "status": "ok", "database": str(self.db_path),
            "age_hours": age_hours, "stale": age_hours is not None and age_hours > STALE_AFTER_HOURS,
            "current_week": s.league.current_week, "warnings": s.warnings,
            "recent_runs": [
                {k: r[k] for k in ("run_id", "status", "started_at", "finished_at", "error")}
                for r in self.store.list_runs(s.league.league_key, limit=5)
            ],
            "refresh_command": f"python utils/sync_yahoo_league.py --league-id {s.league.league_id}",
            "sync_in_progress": in_progress,
        }
        runs = result["recent_runs"]
        if runs and runs[0]["status"] == "failed":
            result["last_attempt_failed"] = runs[0]["error"]
            result["sync_log_tail"] = log_tail()
            if "Login required" in (runs[0]["error"] or ""):
                result["action_needed"] = (
                    "Yahoo login expired. Run: python utils/yahoo_browser_login.py "
                    f"--league-id {s.league.league_id} --manual-login"
                )
        return result
