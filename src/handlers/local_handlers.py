"""MCP tool handlers backed by the local synced league store (DATA_SOURCE=local).

Same tool names and arguments as the Yahoo API handlers; data comes from the SQLite
store filled by ``utils/sync_yahoo_league.py``. Roster enrichment (Sleeper, tiers) and the
waiver-wire analysis reuse the shared API-handler functions; ``ff_build_lineup`` uses the
league-aware optimizer in ``src/analysis/lineup.py``.
"""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

from src.analysis.lineup import optimize_lineup
from src.datasource.local_source import LocalLeagueSource, NoLocalData, normalize_league_key
from src.datasource.sync_trigger import start_background_sync
from src.handlers import player_handlers
from src.handlers.roster_handlers import enhance_roster_result, roster_detail_flags

Handler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]

_source: Optional[LocalLeagueSource] = None


def get_source() -> LocalLeagueSource:
    global _source
    if _source is None:
        _source = LocalLeagueSource()
    return _source


def set_source(source: Optional[LocalLeagueSource]) -> None:
    """Replace the data source (tests, alternate database paths)."""
    global _source
    _source = source


def _handles_missing_data(fn: Handler) -> Handler:
    @functools.wraps(fn)
    async def wrapper(arguments: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return await fn(arguments)
        except NoLocalData as exc:
            return {
                "status": "error",
                "error": str(exc),
                "data_source": "local",
                "suggestion": "Sync league data: python utils/sync_yahoo_league.py --league-id <id>",
            }

    return wrapper


def _int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------- existing tools


@_handles_missing_data
async def handle_ff_get_leagues(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().leagues()


@_handles_missing_data
async def handle_ff_get_league_info(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().league_info(arguments.get("league_key"))


@_handles_missing_data
async def handle_ff_get_standings(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().standings(arguments.get("league_key"))


@_handles_missing_data
async def handle_ff_get_teams(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().teams(arguments.get("league_key"))


@_handles_missing_data
async def handle_ff_get_roster(arguments: Dict[str, Any]) -> Dict[str, Any]:
    source = get_source()
    league_key, team_key = arguments.get("league_key"), arguments.get("team_key")
    result = source.roster(league_key, team_key)
    data_level, projections, external, analysis = roster_detail_flags(arguments)
    if not (projections or external or analysis):
        return result
    legacy = source.legacy_roster(league_key, result["team_key"])
    week = _int(arguments.get("week"), result.get("week"))
    return await enhance_roster_result(
        result, legacy, result["league_key"], result["team_key"], week, data_level, projections, external, analysis
    )


@_handles_missing_data
async def handle_ff_get_matchup(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().my_matchup(
        arguments.get("league_key"), _int(arguments.get("week")), arguments.get("team_key")
    )


@_handles_missing_data
async def handle_ff_compare_teams(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().compare_teams(
        arguments.get("league_key"), arguments.get("team_key_a"), arguments.get("team_key_b")
    )


@_handles_missing_data
async def handle_ff_build_lineup(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """League-aware optimizer: your league's slots, Yahoo projections, injury status, byes."""
    source = get_source()
    snapshot = source.snapshot(arguments.get("league_key"))
    team_key = source._team_key(snapshot, arguments.get("team_key"))
    requested_week = _int(arguments.get("week"))
    result = optimize_lineup(
        snapshot,
        team_key,
        strategy=arguments.get("strategy", "balanced"),
        include_waivers=arguments.get("include_waivers", True) is not False,
    )
    result.update(
        status="success",
        league_key=snapshot.league.league_key,
        team_name=snapshot.team(team_key).name,
        synced_at=snapshot.captured_at,
        data_source="local",
    )
    if requested_week and requested_week != snapshot.league.current_week:
        result["note"] = (
            f"Projections are synced for week {snapshot.league.current_week} only; "
            f"week {requested_week} was not optimized."
        )
    return result


@_handles_missing_data
async def handle_ff_get_players(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().available_players(
        arguments.get("league_key"),
        arguments.get("position"),
        arguments.get("sort", "rank"),
        _int(arguments.get("count"), 25),
        arguments.get("availability"),
    )


@_handles_missing_data
async def handle_ff_get_waiver_wire(arguments: Dict[str, Any]) -> Dict[str, Any]:
    # The API handler's enrichment runs on players from the injected local getter.
    result = await player_handlers.handle_ff_get_waiver_wire(arguments)
    if isinstance(result, dict):
        status = get_source().sync_status(arguments.get("league_key"))
        result.setdefault("synced_at", status.get("synced_at"))
        result.setdefault("data_source", "local")
    return result


async def local_waiver_wire_players(league_key: str, position: str = "all", sort: str = "rank",
                                    count: int = 30) -> list:
    """Injected into player_handlers as ``get_waiver_wire_players`` in local mode."""
    try:
        return get_source().legacy_waiver_players(league_key, position, sort, count)
    except NoLocalData:
        return []


@_handles_missing_data
async def handle_ff_get_draft_results(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().draft_results(arguments.get("league_key"), arguments.get("team_key"))


async def _needs_yahoo_api(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "unavailable",
        "data_source": "local",
        "message": "This tool needs the official Yahoo Fantasy API (pre-draft rankings/ADP). "
                   "It is not available from synced league data. Set DATA_SOURCE=yahoo_api once "
                   "Yahoo approves API access.",
    }


async def handle_ff_refresh_token(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "not_applicable",
        "data_source": "local",
        "message": "Local mode reads synced league data; no Yahoo OAuth token is used. "
                   "To refresh data run: python utils/sync_yahoo_league.py --league-id <id>",
    }


async def handle_ff_get_api_status(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().sync_status(arguments.get("league_key"))


async def handle_ff_clear_cache(arguments: Dict[str, Any]) -> Dict[str, Any]:
    source = get_source()
    source._cache.clear()
    return {"status": "success", "data_source": "local",
            "message": "Local cache cleared; data reloads from the database (it also reloads after every sync)."}


# ------------------------------------------------------------------------ new tools


@_handles_missing_data
async def handle_ff_get_league_settings(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().league_settings(arguments.get("league_key"))


@_handles_missing_data
async def handle_ff_get_all_rosters(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().all_rosters(arguments.get("league_key"), arguments.get("position"))


@_handles_missing_data
async def handle_ff_get_matchups(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().matchups(arguments.get("league_key"), _int(arguments.get("week")))


@_handles_missing_data
async def handle_ff_get_transactions(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().transactions(
        arguments.get("league_key"),
        team_key=arguments.get("team_key"),
        type=arguments.get("type"),
        player=arguments.get("player"),
        since=arguments.get("since"),
        limit=_int(arguments.get("limit"), 50),
    )


@_handles_missing_data
async def handle_ff_get_faab_history(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().faab_bids(arguments.get("league_key"), arguments.get("team_key"))


@_handles_missing_data
async def handle_ff_search_players(arguments: Dict[str, Any]) -> Dict[str, Any]:
    query = (arguments.get("query") or "").strip()
    if not query:
        return {"status": "error", "error": "query is required"}
    return get_source().search_players(arguments.get("league_key"), query, _int(arguments.get("limit"), 10))


@_handles_missing_data
async def handle_ff_get_player_history(arguments: Dict[str, Any]) -> Dict[str, Any]:
    player_key = (arguments.get("player_key") or "").strip()
    if not player_key:
        return {"status": "error", "error": "player_key is required (use ff_search_players to find it)"}
    return get_source().player_history(arguments.get("league_key"), player_key)


async def handle_ff_get_sync_status(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return get_source().sync_status(arguments.get("league_key"))


@_handles_missing_data
async def handle_ff_get_player_details(arguments: Dict[str, Any]) -> Dict[str, Any]:
    player = (arguments.get("player") or arguments.get("player_key") or "").strip()
    if not player:
        return {"status": "error", "error": "player is required (name or player_key)"}
    # Sleeper calls are blocking HTTP; keep the event loop free.
    return await asyncio.to_thread(get_source().player_details, arguments.get("league_key"), player)


async def handle_ff_sync_league(arguments: Dict[str, Any]) -> Dict[str, Any]:
    source = get_source()
    league_key = normalize_league_key(arguments.get("league_key"))
    last_synced = None
    try:
        snapshot = source.snapshot(league_key)
        league_key = snapshot.league.league_key
        last_synced = datetime.fromisoformat(snapshot.captured_at).timestamp()
    except (NoLocalData, ValueError, TypeError):
        pass  # first sync, or unknown freshness: allowed
    if not league_key:
        return {"status": "error", "error": "league_key is required for the first sync"}
    last_attempt = None
    try:
        runs = source.store.list_runs(league_key, limit=1)
        if runs:
            last_attempt = datetime.fromisoformat(runs[0]["finished_at"]).timestamp()
    except (NoLocalData, ValueError, TypeError):
        pass
    return start_background_sync(
        league_key.rsplit(".", 1)[-1],
        mode=arguments.get("mode") or "full",
        force=bool(arguments.get("force")),
        last_synced_epoch=last_synced,
        last_attempt_epoch=last_attempt,
    )


LOCAL_TOOL_HANDLERS: Dict[str, Handler] = {
    "ff_get_leagues": handle_ff_get_leagues,
    "ff_get_league_info": handle_ff_get_league_info,
    "ff_get_standings": handle_ff_get_standings,
    "ff_get_teams": handle_ff_get_teams,
    "ff_get_roster": handle_ff_get_roster,
    "ff_get_roster_with_projections": handle_ff_get_roster,
    "ff_get_matchup": handle_ff_get_matchup,
    "ff_get_players": handle_ff_get_players,
    "ff_compare_teams": handle_ff_compare_teams,
    "ff_build_lineup": handle_ff_build_lineup,
    "ff_refresh_token": handle_ff_refresh_token,
    "ff_get_api_status": handle_ff_get_api_status,
    "ff_clear_cache": handle_ff_clear_cache,
    "ff_get_draft_results": handle_ff_get_draft_results,
    "ff_get_waiver_wire": handle_ff_get_waiver_wire,
    "ff_get_draft_rankings": _needs_yahoo_api,
    "ff_get_draft_recommendation": _needs_yahoo_api,
    "ff_analyze_draft_state": _needs_yahoo_api,
    # New tools (local data only)
    "ff_get_league_settings": handle_ff_get_league_settings,
    "ff_get_all_rosters": handle_ff_get_all_rosters,
    "ff_get_matchups": handle_ff_get_matchups,
    "ff_get_transactions": handle_ff_get_transactions,
    "ff_get_faab_history": handle_ff_get_faab_history,
    "ff_search_players": handle_ff_search_players,
    "ff_get_player_history": handle_ff_get_player_history,
    "ff_get_sync_status": handle_ff_get_sync_status,
    "ff_get_player_details": handle_ff_get_player_details,
    "ff_sync_league": handle_ff_sync_league,
}

_LEAGUE_KEY = {"type": "string", "description": "League key, e.g. 'nfl.l.269337' (from ff_get_leagues)"}
_TEAM_KEY = {"type": "string", "description": "Team key, e.g. 'nfl.l.269337.t.4', or team id '4'"}

# Descriptions/schemas for the tools only the local store can answer (used by both servers).
LOCAL_TOOL_SPECS: Dict[str, Dict[str, Any]] = {
    "ff_get_league_settings": {
        "description": "League rules: scoring (every stat's point value, league vs Yahoo default), roster "
                       "positions, FLEX, bench/IR, waiver/FAAB rules, trade deadline, playoff format.",
        "input_schema": {"type": "object", "properties": {"league_key": _LEAGUE_KEY}, "required": ["league_key"]},
    },
    "ff_get_all_rosters": {
        "description": "Every team's current roster in one call (compact): slot, positions, injury status, "
                       "projection, % rostered. Optional position filter (QB/RB/WR/TE/K/DEF/W/R/T) to compare "
                       "one position group across the league.",
        "input_schema": {"type": "object", "properties": {
            "league_key": _LEAGUE_KEY, "position": {"type": "string", "description": "Optional position filter"},
        }, "required": ["league_key"]},
    },
    "ff_get_matchups": {
        "description": "All matchups for a week (default current): teams, points, projected points, status, winner. "
                       "Past weeks give final results.",
        "input_schema": {"type": "object", "properties": {
            "league_key": _LEAGUE_KEY, "week": {"type": "integer", "description": "Week number (optional)"},
        }, "required": ["league_key"]},
    },
    "ff_get_transactions": {
        "description": "League transaction history (adds, drops, trades) newest first, with FAAB paid. Filter by "
                       "team, type ('add', 'drop', 'trade'), player name/key, or since (ISO date).",
        "input_schema": {"type": "object", "properties": {
            "league_key": _LEAGUE_KEY, "team_key": _TEAM_KEY,
            "type": {"type": "string"}, "player": {"type": "string"},
            "since": {"type": "string", "description": "ISO date, e.g. '2026-09-15'"},
            "limit": {"type": "integer", "default": 50},
        }, "required": ["league_key"]},
    },
    "ff_get_faab_history": {
        "description": "Processed FAAB waiver claims with EVERY bid (winner and losers, amounts, reasons) plus a "
                       "per-manager summary (claims won, FAAB spent, bids placed, max bid, FAAB remaining). Use for "
                       "bid sizing and manager tendencies.",
        "input_schema": {"type": "object", "properties": {"league_key": _LEAGUE_KEY, "team_key": _TEAM_KEY},
                         "required": ["league_key"]},
    },
    "ff_search_players": {
        "description": "Find players by name: who owns them in this league (team) or whether they are on "
                       "waivers/free agents, plus projections and status. Returns player_key for other tools.",
        "input_schema": {"type": "object", "properties": {
            "league_key": _LEAGUE_KEY, "query": {"type": "string", "description": "Name or part of a name"},
            "limit": {"type": "integer", "default": 10},
        }, "required": ["league_key", "query"]},
    },
    "ff_get_player_history": {
        "description": "One player's league history: which fantasy team held them at each sync, related "
                       "transactions, and where they were drafted.",
        "input_schema": {"type": "object", "properties": {
            "league_key": _LEAGUE_KEY, "player_key": {"type": "string", "description": "e.g. 'nfl.p.40900'"},
        }, "required": ["league_key", "player_key"]},
    },
    "ff_get_sync_status": {
        "description": "When league data was last synced from Yahoo, whether it is stale, whether a sync is "
                       "running now, the last failure (with log tail), and recent sync runs.",
        "input_schema": {"type": "object", "properties": {"league_key": _LEAGUE_KEY}},
    },
    "ff_get_player_details": {
        "description": "Deep dive on ONE player: league context (owner or waiver status, Yahoo injury status, "
                       "projections) plus Sleeper data — week-by-week game log (fantasy points, snaps and snap "
                       "share, targets, red-zone looks, carries, yards), season averages, injury body part/notes, "
                       "depth-chart position, age. Live call to Sleeper's public API.",
        "input_schema": {"type": "object", "properties": {
            "league_key": _LEAGUE_KEY,
            "player": {"type": "string", "description": "Player name or player_key (e.g. 'nfl.p.40900')"},
        }, "required": ["league_key", "player"]},
    },
    "ff_sync_league": {
        "description": "Refresh league data from Yahoo in the background (takes 1.5-3 min; a Chrome window may "
                       "open). mode 'quick' = rosters, standings, matchups, waivers; 'full' adds transactions, "
                       "FAB bids, draft, past weeks. Refuses if a sync is running or the last one was <10 min ago "
                       "(force=true overrides). Check ff_get_sync_status for completion.",
        "input_schema": {"type": "object", "properties": {
            "league_key": _LEAGUE_KEY,
            "mode": {"type": "string", "enum": ["quick", "full"], "default": "full"},
            "force": {"type": "boolean", "default": False},
        }, "required": ["league_key"]},
    },
}
LOCAL_ONLY_TOOLS = tuple(LOCAL_TOOL_SPECS)
