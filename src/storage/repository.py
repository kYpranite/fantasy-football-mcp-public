"""Save and load league snapshots in the local SQLite store.

``save_snapshot`` writes a whole sync in ONE transaction: if anything fails, nothing is
written and the previous committed run stays "current". Failed syncs are recorded
separately (``record_failed_sync``) without touching league data.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.models.league_data import (
    AvailablePlayer,
    DraftPick,
    League,
    LeagueSettings,
    LeagueSnapshot,
    Matchup,
    MatchupSide,
    PlayerScan,
    PlayoffSettings,
    RosterEntry,
    ScoringRule,
    Standing,
    Team,
    TeamRoster,
    Transaction,
    TransactionPlayer,
    WaiverBid,
    WaiverClaim,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def claim_id(claim: WaiverClaim) -> str:
    """Yahoo shows no claim id; derive a stable one."""
    fingerprint = f"{claim.player_key}|{claim.timestamp_raw}|{claim.awarded_team_key}"
    return hashlib.sha1(fingerprint.encode()).hexdigest()[:12]


class LeagueStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------------ writing

    def save_snapshot(
        self,
        snapshot: LeagueSnapshot,
        started_at: Optional[str] = None,
        include_players: bool = True,
        include_history: bool = True,
    ) -> int:
        """Persist a validated snapshot atomically; returns the new run_id."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            run_id = conn.execute(
                """INSERT INTO sync_runs (league_key, source, status, started_at, captured_at, finished_at,
                                          include_players, include_history, warnings_json)
                   VALUES (?, ?, 'committed', ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.league.league_key,
                    snapshot.source,
                    started_at or snapshot.captured_at,
                    snapshot.captured_at,
                    _now(),
                    int(include_players),
                    int(include_history),
                    _json(snapshot.warnings),
                ),
            ).lastrowid
            self._write_league(run_id, snapshot)
            self._write_current_state(run_id, snapshot)
            self._write_history(run_id, snapshot)
            conn.execute("COMMIT")
            return run_id
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def record_failed_sync(self, league_key: str, source: str, started_at: str, error: str) -> int:
        """Log a failed sync attempt; league data is untouched."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            run_id = conn.execute(
                """INSERT INTO sync_runs (league_key, source, status, started_at, finished_at, error)
                   VALUES (?, ?, 'failed', ?, ?, ?)""",
                (league_key, source, started_at, _now(), error[:4000]),
            ).lastrowid
            conn.execute("COMMIT")
            return run_id
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _write_league(self, run_id: int, s: LeagueSnapshot) -> None:
        league = s.league
        self.conn.execute(
            """INSERT INTO leagues (league_key, league_id, name, season, url, updated_run_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (league_key) DO UPDATE SET name = excluded.name, season = excluded.season,
                   url = excluded.url, updated_run_id = excluded.updated_run_id""",
            (league.league_key, league.league_id, league.name, league.season, league.url, run_id),
        )
        settings = asdict(s.settings)
        settings.pop("scoring")  # stored as rows in scoring_rules
        self.conn.execute(
            """INSERT INTO league_state (run_id, league_key, name, season, current_week, num_teams,
                                         settings_json, player_scans_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, league.league_key, league.name, league.season, league.current_week,
                league.num_teams, _json(settings), _json([asdict(x) for x in s.player_scans]),
            ),
        )
        self.conn.executemany(
            """INSERT INTO scoring_rules (run_id, seq, category, stat, raw_value, points, yards_per_point,
                                          yahoo_default) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (run_id, i, r.category, r.stat, r.raw_value, r.points, r.yards_per_point, r.yahoo_default)
                for i, r in enumerate(s.settings.scoring)
            ],
        )

    def _write_current_state(self, run_id: int, s: LeagueSnapshot) -> None:
        conn = self.conn
        conn.executemany(
            """INSERT INTO teams (run_id, team_key, seq, team_id, name, is_mine, managers_json,
                                  is_commissioner, faab_remaining, waiver_priority, moves, trades, last_activity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (run_id, t.team_key, i, t.team_id, t.name, int(t.is_mine), _json(t.managers),
                 int(t.is_commissioner), t.faab_remaining, t.waiver_priority, t.moves, t.trades, t.last_activity)
                for i, t in enumerate(s.teams)
            ],
        )
        conn.executemany(
            """INSERT INTO standings (run_id, team_key, rank, wins, losses, ties, points_for, points_against, streak)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (run_id, x.team_key, x.rank, x.wins, x.losses, x.ties, x.points_for, x.points_against, x.streak)
                for x in s.standings
            ],
        )
        for roster in s.rosters:
            conn.execute(
                "INSERT INTO rosters (run_id, team_key, week, open_slots_json) VALUES (?, ?, ?, ?)",
                (run_id, roster.team_key, roster.week, _json(roster.empty_slots)),
            )
            for i, p in enumerate(roster.players):
                self._upsert_player(run_id, p.player_key, p.player_id, p.name, p.nfl_team, p.positions, True)
                conn.execute(
                    """INSERT INTO roster_entries (run_id, player_key, team_key, seq, slot, status, status_full,
                           bye_week, fantasy_points, projected_points, percent_started, percent_rostered, game)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, p.player_key, roster.team_key, i, p.slot, p.status, p.status_full, p.bye_week,
                     p.fantasy_points, p.projected_points, p.percent_started, p.percent_rostered, p.game),
                )
        for i, p in enumerate(s.available_players):
            self._upsert_player(run_id, p.player_key, p.player_id, p.name, p.nfl_team, p.positions, True)
            conn.execute(
                """INSERT INTO available_players (run_id, player_key, seq, availability, waiver_until, status,
                       status_full, bye_week, games_played, percent_rostered, preseason_rank, current_rank,
                       projected_week, projected_rest_of_season, season_points, game)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, p.player_key, i, p.availability, p.waiver_until, p.status, p.status_full, p.bye_week,
                 p.games_played, p.percent_rostered, p.preseason_rank, p.current_rank, p.projected_week,
                 p.projected_rest_of_season, p.season_points, p.game),
            )

    def _write_history(self, run_id: int, s: LeagueSnapshot) -> None:
        conn = self.conn
        league_key = s.league.league_key
        for m in list(s.matchup_history) + list(s.matchups):
            if len(m.teams) != 2:
                continue
            for side, (me, them) in enumerate([(m.teams[0], m.teams[1]), (m.teams[1], m.teams[0])]):
                conn.execute(
                    """INSERT INTO matchups (league_key, week, team_key, side, opponent_team_key, points,
                                             projected_points, status, updated_run_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (league_key, week, team_key) DO UPDATE SET
                           side = excluded.side, opponent_team_key = excluded.opponent_team_key,
                           points = excluded.points, projected_points = excluded.projected_points,
                           status = excluded.status, updated_run_id = excluded.updated_run_id""",
                    (league_key, m.week, me.team_key, side, them.team_key, me.points, me.projected_points,
                     m.status, run_id),
                )
        for t in s.transactions:
            inserted = conn.execute(
                """INSERT OR IGNORE INTO transactions (transaction_id, league_key, type, team_key, timestamp,
                                                       timestamp_raw, first_seen_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (t.transaction_id, league_key, t.type, t.team_key, t.timestamp, t.timestamp_raw, run_id),
            ).rowcount
            for i, p in enumerate(t.players):
                self._upsert_player(run_id, p.player_key, p.player_id, p.name, p.nfl_team, p.positions, False)
                if inserted:
                    conn.execute(
                        """INSERT INTO transaction_players (transaction_id, seq, player_key, action, detail, faab_bid)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (t.transaction_id, i, p.player_key, p.action, p.detail, p.faab_bid),
                    )
        for c in s.waiver_claims:
            cid = claim_id(c)
            self._upsert_player(run_id, c.player_key, c.player_id, c.name, c.nfl_team, c.positions, False)
            inserted = conn.execute(
                """INSERT OR IGNORE INTO waiver_claims (claim_id, league_key, player_key, awarded_team_key,
                                                        winning_bid, timestamp, timestamp_raw, first_seen_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, league_key, c.player_key, c.awarded_team_key, c.winning_bid, c.timestamp,
                 c.timestamp_raw, run_id),
            ).rowcount
            if inserted:
                conn.executemany(
                    "INSERT INTO waiver_bids (claim_id, seq, team_key, bid, result) VALUES (?, ?, ?, ?, ?)",
                    [(cid, i, b.team_key, b.bid, b.result) for i, b in enumerate(c.bids)],
                )
        for d in s.draft_picks:
            position = [d.position] if d.position else []
            self._upsert_player(run_id, d.player_key, d.player_id, d.name, d.nfl_team, position, False)
            conn.execute(
                """INSERT INTO draft_picks (league_key, overall, round, pick, team_key, team_name, player_key,
                                            nfl_team, position, updated_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (league_key, overall) DO UPDATE SET round = excluded.round, pick = excluded.pick,
                       team_key = excluded.team_key, team_name = excluded.team_name,
                       player_key = excluded.player_key, nfl_team = excluded.nfl_team,
                       position = excluded.position, updated_run_id = excluded.updated_run_id""",
                (league_key, d.overall, d.round, d.pick, d.team_key, d.team_name, d.player_key, d.nfl_team,
                 d.position, run_id),
            )

    def _upsert_player(
        self, run_id: int, key: str, player_id: str, name: str, nfl_team: Optional[str],
        positions: List[str], authoritative: bool,
    ) -> None:
        """Insert a player; roster/available lists (``authoritative``) refresh name/team/positions."""
        if authoritative:
            self.conn.execute(
                """INSERT INTO players (player_key, player_id, name, nfl_team, positions_json,
                                        first_seen_run_id, last_seen_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (player_key) DO UPDATE SET name = excluded.name, nfl_team = excluded.nfl_team,
                       positions_json = excluded.positions_json, last_seen_run_id = excluded.last_seen_run_id""",
                (key, player_id, name, nfl_team, _json(positions), run_id, run_id),
            )
        else:
            self.conn.execute(
                """INSERT INTO players (player_key, player_id, name, nfl_team, positions_json,
                                        first_seen_run_id, last_seen_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (player_key) DO NOTHING""",
                (key, player_id, name, nfl_team, _json(positions), run_id, run_id),
            )

    # ------------------------------------------------------------------------ reading

    def latest_run_id(self, league_key: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT run_id FROM latest_runs WHERE league_key = ?", (league_key,)
        ).fetchone()
        return row["run_id"] if row else None

    def league_keys(self) -> List[str]:
        return [r["league_key"] for r in self.conn.execute("SELECT league_key FROM latest_runs ORDER BY league_key")]

    def list_runs(self, league_key: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sync_runs"
        args: tuple = ()
        if league_key:
            sql += " WHERE league_key = ?"
            args = (league_key,)
        rows = self.conn.execute(sql + " ORDER BY run_id DESC LIMIT ?", args + (limit,)).fetchall()
        return [dict(r) for r in rows]

    def player_roster_history(self, player_key: str) -> List[Dict[str, Any]]:
        """Which team (or none) held a player at each committed run: roster moves over time."""
        rows = self.conn.execute(
            """SELECT r.run_id, r.captured_at, e.team_key, e.slot
               FROM sync_runs r
               LEFT JOIN roster_entries e ON e.run_id = r.run_id AND e.player_key = ?
               WHERE r.status = 'committed'
               ORDER BY r.run_id""",
            (player_key,),
        ).fetchall()
        return [dict(r) for r in rows]

    def load_snapshot(self, league_key: str, run_id: Optional[int] = None) -> Optional[LeagueSnapshot]:
        """Rebuild a LeagueSnapshot: per-run state from ``run_id`` (default latest committed),
        plus all merged history known for the league."""
        conn = self.conn
        run_id = run_id or self.latest_run_id(league_key)
        if run_id is None:
            return None
        run = conn.execute(
            "SELECT * FROM sync_runs WHERE run_id = ? AND status = 'committed'", (run_id,)
        ).fetchone()
        state = conn.execute("SELECT * FROM league_state WHERE run_id = ?", (run_id,)).fetchone()
        league_row = conn.execute("SELECT * FROM leagues WHERE league_key = ?", (league_key,)).fetchone()
        if run is None or state is None:
            return None

        settings_data = json.loads(state["settings_json"])
        settings_data["playoffs"] = PlayoffSettings(**settings_data["playoffs"])
        settings = LeagueSettings(
            **settings_data,
            scoring=[
                ScoringRule(r["category"], r["stat"], r["raw_value"], r["points"], r["yards_per_point"],
                            r["yahoo_default"])
                for r in conn.execute("SELECT * FROM scoring_rules WHERE run_id = ? ORDER BY seq", (run_id,))
            ],
        )
        teams = [
            Team(r["team_key"], r["team_id"], r["name"], bool(r["is_mine"]), json.loads(r["managers_json"]),
                 bool(r["is_commissioner"]), r["faab_remaining"], r["waiver_priority"], r["moves"], r["trades"],
                 r["last_activity"])
            for r in conn.execute("SELECT * FROM teams WHERE run_id = ? ORDER BY seq", (run_id,))
        ]
        order = {t.team_key: i for i, t in enumerate(teams)}
        standings = sorted(
            (
                Standing(r["team_key"], r["rank"], r["wins"], r["losses"], r["ties"], r["points_for"],
                         r["points_against"], r["streak"])
                for r in conn.execute("SELECT * FROM standings WHERE run_id = ?", (run_id,))
            ),
            key=lambda x: order.get(x.team_key, len(order)),
        )
        rosters = []
        for r in conn.execute("SELECT * FROM rosters WHERE run_id = ?", (run_id,)):
            players = [
                RosterEntry(
                    e["team_key"], e["player_key"], e["player_id"], e["name"], e["slot"], e["nfl_team"],
                    json.loads(e["positions_json"]), e["status"], e["status_full"], e["bye_week"],
                    e["fantasy_points"], e["projected_points"], e["percent_started"], e["percent_rostered"],
                    e["game"],
                )
                for e in conn.execute(
                    """SELECT e.*, p.player_id, p.name, p.nfl_team, p.positions_json
                       FROM roster_entries e JOIN players p USING (player_key)
                       WHERE e.run_id = ? AND e.team_key = ? ORDER BY e.seq""",
                    (run_id, r["team_key"]),
                )
            ]
            rosters.append(TeamRoster(r["team_key"], r["week"], players, json.loads(r["open_slots_json"])))
        rosters.sort(key=lambda x: order.get(x.team_key, len(order)))
        available = [
            AvailablePlayer(
                a["player_key"], a["player_id"], a["name"], a["availability"], a["waiver_until"], a["nfl_team"],
                json.loads(a["positions_json"]), a["status"], a["status_full"], a["bye_week"], a["games_played"],
                a["percent_rostered"], a["preseason_rank"], a["current_rank"], a["projected_week"],
                a["projected_rest_of_season"], a["season_points"], a["game"],
            )
            for a in conn.execute(
                """SELECT a.*, p.player_id, p.name, p.nfl_team, p.positions_json
                   FROM available_players a JOIN players p USING (player_key)
                   WHERE a.run_id = ? ORDER BY a.seq""",
                (run_id,),
            )
        ]
        current_week = state["current_week"]
        all_matchups = self._matchups(league_key)
        return LeagueSnapshot(
            captured_at=run["captured_at"],
            source=run["source"],
            league=League(
                league_key=league_key,
                league_id=league_row["league_id"] if league_row else league_key.rsplit(".", 1)[-1],
                name=state["name"],
                season=state["season"],
                current_week=current_week,
                num_teams=state["num_teams"],
                url=league_row["url"] if league_row else "",
            ),
            settings=settings,
            teams=teams,
            standings=standings,
            rosters=rosters,
            matchups=[m for m in all_matchups if m.week == current_week],
            available_players=available,
            player_scans=[PlayerScan(**x) for x in json.loads(state["player_scans_json"])],
            matchup_history=[m for m in all_matchups if current_week is not None and m.week < current_week],
            transactions=self._transactions(league_key),
            waiver_claims=self._waiver_claims(league_key),
            draft_picks=self._draft_picks(league_key),
            warnings=json.loads(run["warnings_json"]),
        )

    def _matchups(self, league_key: str) -> List[Matchup]:
        rows = self.conn.execute(
            "SELECT * FROM matchups WHERE league_key = ? AND side = 0 ORDER BY week, rowid", (league_key,)
        ).fetchall()
        result = []
        for r in rows:
            other = self.conn.execute(
                "SELECT * FROM matchups WHERE league_key = ? AND week = ? AND team_key = ?",
                (league_key, r["week"], r["opponent_team_key"]),
            ).fetchone()
            sides = [MatchupSide(r["team_key"], r["points"], r["projected_points"])]
            if other is not None:
                sides.append(MatchupSide(other["team_key"], other["points"], other["projected_points"]))
            result.append(Matchup(week=r["week"], status=r["status"], teams=sides))
        return result

    def _transactions(self, league_key: str) -> List[Transaction]:
        result = []
        for t in self.conn.execute(
            "SELECT * FROM transactions WHERE league_key = ? ORDER BY timestamp DESC, rowid", (league_key,)
        ):
            players = [
                TransactionPlayer(p["player_key"], p["player_id"], p["name"], p["action"], p["detail"],
                                  p["faab_bid"], p["nfl_team"], json.loads(p["positions_json"]))
                for p in self.conn.execute(
                    """SELECT tp.*, pl.player_id, pl.name, pl.nfl_team, pl.positions_json
                       FROM transaction_players tp JOIN players pl USING (player_key)
                       WHERE tp.transaction_id = ? ORDER BY tp.seq""",
                    (t["transaction_id"],),
                )
            ]
            result.append(Transaction(t["transaction_id"], t["type"], t["team_key"], t["timestamp"],
                                      t["timestamp_raw"], players))
        return result

    def _waiver_claims(self, league_key: str) -> List[WaiverClaim]:
        result = []
        for c in self.conn.execute(
            """SELECT c.*, p.player_id, p.name, p.nfl_team, p.positions_json
               FROM waiver_claims c JOIN players p USING (player_key)
               WHERE c.league_key = ? ORDER BY c.timestamp DESC, c.rowid""",
            (league_key,),
        ):
            bids = [
                WaiverBid(b["team_key"], b["bid"], b["result"])
                for b in self.conn.execute(
                    "SELECT * FROM waiver_bids WHERE claim_id = ? ORDER BY seq", (c["claim_id"],)
                )
            ]
            result.append(WaiverClaim(c["player_key"], c["player_id"], c["name"], c["awarded_team_key"],
                                      c["winning_bid"], c["timestamp"], c["timestamp_raw"], bids, c["nfl_team"],
                                      json.loads(c["positions_json"])))
        return result

    def _draft_picks(self, league_key: str) -> List[DraftPick]:
        return [
            DraftPick(d["round"], d["pick"], d["overall"], d["team_key"], d["team_name"], d["player_key"],
                      d["player_id"], d["name"], d["nfl_team"], d["position"])
            for d in self.conn.execute(
                """SELECT d.*, p.player_id, p.name
                   FROM draft_picks d JOIN players p USING (player_key)
                   WHERE d.league_key = ? ORDER BY d.overall""",
                (league_key,),
            )
        ]
