"""On-demand player detail from Sleeper's public API (no key needed).

Adds what the synced Yahoo data lacks: week-by-week game logs (fantasy points, snaps,
targets, red-zone looks, carries, yards), injury detail, depth-chart position, age.
Yahoo players are matched to Sleeper by Sleeper's stored ``yahoo_id`` first, then by
normalized name + position (+ team as a tiebreak).

Sources:
- ``https://api.sleeper.app/v1/players/nfl`` — player database (~15 MB), cached on disk for 24 h
- ``https://api.sleeper.com/stats/nfl/player/<id>?season=<y>&season_type=regular&grouping=week``
  — per-player weekly stats (undocumented but public; failures are reported, not hidden)
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.storage.db import PROJECT_ROOT

PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
STATS_URL = "https://api.sleeper.com/stats/nfl/player/{player_id}?season_type=regular&season={season}&grouping=week"
CACHE_PATH = PROJECT_ROOT / "data" / "cache" / "sleeper_players.json"
PLAYERS_MAX_AGE_S = 24 * 3600
TIMEOUT_S = 30

# Stats worth showing per position (Sleeper keys → short labels).
USAGE_STATS: Dict[str, List[str]] = {
    "QB": ["pass_att", "pass_cmp", "pass_yd", "pass_td", "pass_int", "rush_att", "rush_yd", "rush_td"],
    "RB": ["rush_att", "rush_yd", "rush_td", "rec_tgt", "rec", "rec_yd", "rec_td", "rush_rz_att", "rec_rz_tgt"],
    "WR": ["rec_tgt", "rec", "rec_yd", "rec_td", "rec_air_yd", "rec_rz_tgt", "rush_att", "rush_yd"],
    "TE": ["rec_tgt", "rec", "rec_yd", "rec_td", "rec_air_yd", "rec_rz_tgt"],
    "K": ["fgm", "fga", "fgm_lng", "xpm", "xpa"],
    "DEF": ["pts_allow", "yds_allow", "sack", "int", "fum_rec", "def_td", "safe"],
}
_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


class PlayerDetailsError(RuntimeError):
    """Sleeper data could not be fetched or the player could not be matched."""


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z ]", "", text.replace("-", " "))
    return " ".join(_SUFFIX.sub("", text).split())


def _fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "fantasy-football-mcp"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # network, HTTP, JSON
        raise PlayerDetailsError(f"Sleeper request failed ({url.split('?')[0]}): {exc}") from exc


class SleeperPlayerDetails:
    def __init__(self, cache_path: Path = CACHE_PATH, fetch=_fetch_json):
        self.cache_path = cache_path
        self.fetch = fetch
        self._players: Optional[Dict[str, Dict[str, Any]]] = None
        self._by_yahoo: Dict[str, Dict[str, Any]] = {}
        self._by_name: Dict[str, List[Dict[str, Any]]] = {}

    # ------------------------------------------------------------------ player DB

    def players(self) -> Dict[str, Dict[str, Any]]:
        if self._players is None:
            fresh = self.cache_path.exists() and time.time() - self.cache_path.stat().st_mtime < PLAYERS_MAX_AGE_S
            if fresh:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            else:
                data = self.fetch(PLAYERS_URL)
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(data), encoding="utf-8")
            self._index(data)
        return self._players

    def _index(self, data: Dict[str, Dict[str, Any]]) -> None:
        self._players = data
        for player in data.values():
            if player.get("yahoo_id"):
                self._by_yahoo[str(player["yahoo_id"])] = player
            name = normalize_name(player.get("full_name") or f"{player.get('first_name', '')} {player.get('last_name', '')}")
            if name:
                self._by_name.setdefault(name, []).append(player)

    def match(self, yahoo_id: str, name: str, position: Optional[str], nfl_team: Optional[str]):
        """Return (sleeper_player, match_method) or (None, reason)."""
        players = self.players()
        if position == "DEF" and nfl_team:
            player = players.get(nfl_team)  # Sleeper keys team defenses by abbreviation
            return (player, "team_defense") if player else (None, f"no Sleeper defense for {nfl_team}")
        if yahoo_id and yahoo_id in self._by_yahoo:
            return self._by_yahoo[yahoo_id], "yahoo_id"
        candidates = self._by_name.get(normalize_name(name), [])
        if position:
            same_pos = [p for p in candidates if position in (p.get("fantasy_positions") or [p.get("position")])]
            candidates = same_pos or candidates
        if len(candidates) > 1 and nfl_team:
            same_team = [p for p in candidates if p.get("team") == nfl_team]
            candidates = same_team or candidates
        if len(candidates) == 1:
            return candidates[0], "name_position"
        if not candidates:
            return None, "no Sleeper player with that name"
        return None, f"ambiguous: {len(candidates)} Sleeper players named {name!r}"

    # --------------------------------------------------------------------- details

    def details(self, yahoo_id: str, name: str, position: Optional[str], nfl_team: Optional[str],
                season: int) -> Dict[str, Any]:
        player, method = self.match(yahoo_id, name, position, nfl_team)
        if player is None:
            raise PlayerDetailsError(f"Could not match {name} to Sleeper: {method}")
        profile = {
            "sleeper_id": player.get("player_id"), "match_method": method,
            "team": player.get("team"), "position": player.get("position"),
            "age": player.get("age"), "years_exp": player.get("years_exp"),
            "status": player.get("status"),
            "injury_status": player.get("injury_status"), "injury_body_part": player.get("injury_body_part"),
            "injury_notes": player.get("injury_notes"), "injury_start_date": player.get("injury_start_date"),
            "depth_chart_position": player.get("depth_chart_position"),
            "depth_chart_order": player.get("depth_chart_order"),
        }
        raw = self.fetch(STATS_URL.format(player_id=player["player_id"], season=season)) or {}
        game_log = self._game_log(raw, position or player.get("position") or "")
        return {
            "profile": profile,
            "season": season,
            "points_note": "pts_ppr / pts_half_ppr are Sleeper's standard scoring, not this league's; "
                           "use ff_get_league_settings for league scoring rules.",
            "game_log": game_log,
            "season_summary": self._summary(game_log),
        }

    @staticmethod
    def _game_log(raw: Dict[str, Any], position: str) -> List[Dict[str, Any]]:
        keys = USAGE_STATS.get(position, [])
        log = []
        for week, entry in sorted(raw.items(), key=lambda item: int(item[0]) if str(item[0]).isdigit() else 99):
            if not isinstance(entry, dict):
                continue
            stats = entry.get("stats") or {}
            row: Dict[str, Any] = {
                "week": int(week) if str(week).isdigit() else week,
                "date": entry.get("date"),
                "opponent": entry.get("opponent"),
                "played": bool(stats.get("gp")),
                "pts_ppr": stats.get("pts_ppr"),
                "pts_half_ppr": stats.get("pts_half_ppr"),
                "pos_rank_ppr": stats.get("pos_rank_ppr") if (stats.get("pos_rank_ppr") or 0) < 999 else None,
            }
            if stats.get("off_snp") is not None:
                row["snaps"] = stats.get("off_snp")
                if stats.get("tm_off_snp"):
                    row["snap_share"] = round(stats["off_snp"] / stats["tm_off_snp"], 3)
            row.update({k: stats[k] for k in keys if k in stats})
            log.append(row)
        return log

    @staticmethod
    def _summary(log: List[Dict[str, Any]]) -> Dict[str, Any]:
        played = [g for g in log if g.get("played")]
        points = [g["pts_ppr"] for g in played if g.get("pts_ppr") is not None]
        shares = [g["snap_share"] for g in played if "snap_share" in g]
        targets = [g["rec_tgt"] for g in played if "rec_tgt" in g]
        return {
            "games_played": len(played),
            "total_pts_ppr": round(sum(points), 2) if points else None,
            "avg_pts_ppr": round(sum(points) / len(points), 2) if points else None,
            "last_game_pts_ppr": points[-1] if points else None,
            "avg_snap_share": round(sum(shares) / len(shares), 3) if shares else None,
            "avg_targets": round(sum(targets) / len(targets), 2) if targets else None,
        }
