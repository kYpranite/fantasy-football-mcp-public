"""SQLite schema and migrations for the local league store.

Two kinds of tables:

* **Per-run snapshots** (keyed by ``run_id``): league state, settings, scoring, teams,
  standings, rosters, available players. Every successful sync adds a full set, so
  history such as "when did this player change teams" can be answered by comparing runs.
* **Merged history** (natural keys, upserted): players, matchups, transactions, FAB
  claims and bids, draft picks. Re-syncing never duplicates these.

"Current" data = rows from the latest ``committed`` run (see the ``current_*`` views).
Schema version is tracked in ``PRAGMA user_version``; append new migrations to
``MIGRATIONS`` — never edit an applied one.
"""

from __future__ import annotations

from typing import List

MIGRATION_1 = """
CREATE TABLE sync_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    league_key      TEXT NOT NULL,
    source          TEXT NOT NULL,              -- yahoo_web | yahoo_api
    status          TEXT NOT NULL CHECK (status IN ('committed', 'failed')),
    started_at      TEXT NOT NULL,              -- ISO-8601 UTC
    captured_at     TEXT,                       -- when extraction began (committed runs)
    finished_at     TEXT NOT NULL,
    include_players INTEGER NOT NULL DEFAULT 0,
    include_history INTEGER NOT NULL DEFAULT 0,
    warnings_json   TEXT NOT NULL DEFAULT '[]',
    error           TEXT                        -- failed runs only
);
CREATE INDEX sync_runs_league ON sync_runs (league_key, status, run_id);

CREATE TABLE leagues (
    league_key      TEXT PRIMARY KEY,
    league_id       TEXT NOT NULL,
    name            TEXT NOT NULL,
    season          INTEGER,
    url             TEXT,
    updated_run_id  INTEGER NOT NULL REFERENCES sync_runs (run_id)
);

-- ---------------------------------------------------------------- per-run snapshots

CREATE TABLE league_state (
    run_id          INTEGER PRIMARY KEY REFERENCES sync_runs (run_id) ON DELETE CASCADE,
    league_key      TEXT NOT NULL,
    name            TEXT NOT NULL,
    season          INTEGER,
    current_week    INTEGER,
    num_teams       INTEGER NOT NULL,
    settings_json   TEXT NOT NULL,              -- LeagueSettings (typed fields + raw labels)
    player_scans_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE scoring_rules (
    run_id          INTEGER NOT NULL REFERENCES sync_runs (run_id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    category        TEXT NOT NULL,
    stat            TEXT NOT NULL,
    raw_value       TEXT NOT NULL,
    points          REAL,
    yards_per_point REAL,
    yahoo_default   TEXT,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE teams (
    run_id          INTEGER NOT NULL REFERENCES sync_runs (run_id) ON DELETE CASCADE,
    team_key        TEXT NOT NULL,
    seq             INTEGER NOT NULL,           -- standings display order
    team_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    is_mine         INTEGER NOT NULL,
    managers_json   TEXT NOT NULL,
    is_commissioner INTEGER NOT NULL,
    faab_remaining  REAL,
    waiver_priority INTEGER,
    moves           INTEGER,
    trades          INTEGER,
    last_activity   TEXT,
    PRIMARY KEY (run_id, team_key)
);

CREATE TABLE standings (
    run_id          INTEGER NOT NULL REFERENCES sync_runs (run_id) ON DELETE CASCADE,
    team_key        TEXT NOT NULL,
    rank            INTEGER,
    wins            INTEGER NOT NULL,
    losses          INTEGER NOT NULL,
    ties            INTEGER NOT NULL,
    points_for      REAL,
    points_against  REAL,
    streak          TEXT,
    PRIMARY KEY (run_id, team_key)
);

CREATE TABLE rosters (
    run_id          INTEGER NOT NULL REFERENCES sync_runs (run_id) ON DELETE CASCADE,
    team_key        TEXT NOT NULL,
    week            INTEGER,
    open_slots_json TEXT NOT NULL,
    PRIMARY KEY (run_id, team_key)
);

CREATE TABLE roster_entries (
    run_id          INTEGER NOT NULL REFERENCES sync_runs (run_id) ON DELETE CASCADE,
    player_key      TEXT NOT NULL,              -- a player is on at most one roster per run
    team_key        TEXT NOT NULL,
    seq             INTEGER NOT NULL,           -- order on the team page
    slot            TEXT NOT NULL,
    status          TEXT,
    status_full     TEXT,
    bye_week        INTEGER,
    fantasy_points  REAL,
    projected_points REAL,
    percent_started REAL,
    percent_rostered REAL,
    game            TEXT,
    PRIMARY KEY (run_id, player_key)
);
CREATE INDEX roster_entries_team ON roster_entries (run_id, team_key, seq);
CREATE INDEX roster_entries_player ON roster_entries (player_key, run_id);

CREATE TABLE available_players (
    run_id          INTEGER NOT NULL REFERENCES sync_runs (run_id) ON DELETE CASCADE,
    player_key      TEXT NOT NULL,
    seq             INTEGER NOT NULL,           -- list order (rest-of-season projection first)
    availability    TEXT NOT NULL,
    waiver_until    TEXT,
    status          TEXT,
    status_full     TEXT,
    bye_week        INTEGER,
    games_played    INTEGER,
    percent_rostered REAL,
    preseason_rank  INTEGER,
    current_rank    INTEGER,
    projected_week  REAL,
    projected_rest_of_season REAL,
    season_points   REAL,
    game            TEXT,
    PRIMARY KEY (run_id, player_key)
);

-- ------------------------------------------------------------------ merged history

CREATE TABLE players (
    player_key      TEXT PRIMARY KEY,
    player_id       TEXT NOT NULL,
    name            TEXT NOT NULL,              -- latest name seen (DEF labels vary)
    nfl_team        TEXT,
    positions_json  TEXT NOT NULL DEFAULT '[]',
    first_seen_run_id INTEGER NOT NULL,
    last_seen_run_id  INTEGER NOT NULL
);

CREATE TABLE matchups (
    league_key      TEXT NOT NULL,
    week            INTEGER NOT NULL,
    team_key        TEXT NOT NULL,
    side            INTEGER NOT NULL,           -- 0/1 display order within the matchup
    opponent_team_key TEXT NOT NULL,
    points          REAL,
    projected_points REAL,
    status          TEXT,
    updated_run_id  INTEGER NOT NULL,
    PRIMARY KEY (league_key, week, team_key)
);

CREATE TABLE transactions (
    transaction_id  TEXT PRIMARY KEY,
    league_key      TEXT NOT NULL,
    type            TEXT NOT NULL,
    team_key        TEXT,
    timestamp       TEXT,
    timestamp_raw   TEXT NOT NULL,
    first_seen_run_id INTEGER NOT NULL
);
CREATE INDEX transactions_league_time ON transactions (league_key, timestamp);
CREATE INDEX transactions_team ON transactions (team_key, timestamp);

CREATE TABLE transaction_players (
    transaction_id  TEXT NOT NULL REFERENCES transactions (transaction_id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    player_key      TEXT NOT NULL,
    action          TEXT NOT NULL,
    detail          TEXT,
    faab_bid        REAL,
    PRIMARY KEY (transaction_id, seq)
);
CREATE INDEX transaction_players_player ON transaction_players (player_key);

CREATE TABLE waiver_claims (
    claim_id        TEXT PRIMARY KEY,           -- hash of player, time, awarded team
    league_key      TEXT NOT NULL,
    player_key      TEXT NOT NULL,
    awarded_team_key TEXT,
    winning_bid     REAL,
    timestamp       TEXT,
    timestamp_raw   TEXT NOT NULL,
    first_seen_run_id INTEGER NOT NULL
);

CREATE TABLE waiver_bids (
    claim_id        TEXT NOT NULL REFERENCES waiver_claims (claim_id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,           -- 0 = winning bid
    team_key        TEXT,
    bid             REAL,
    result          TEXT NOT NULL,
    PRIMARY KEY (claim_id, seq)
);
CREATE INDEX waiver_bids_team ON waiver_bids (team_key);

CREATE TABLE draft_picks (
    league_key      TEXT NOT NULL,
    overall         INTEGER NOT NULL,
    round           INTEGER NOT NULL,
    pick            INTEGER NOT NULL,
    team_key        TEXT,
    team_name       TEXT NOT NULL,
    player_key      TEXT NOT NULL,
    nfl_team        TEXT,                       -- as of the draft
    position        TEXT,
    updated_run_id  INTEGER NOT NULL,
    PRIMARY KEY (league_key, overall)
);

-- ------------------------------------------------------------ current-state views

CREATE VIEW latest_runs AS
    SELECT league_key, MAX(run_id) AS run_id
    FROM sync_runs WHERE status = 'committed' GROUP BY league_key;

CREATE VIEW current_teams AS
    SELECT l.league_key, t.* FROM teams t JOIN latest_runs l USING (run_id);

CREATE VIEW current_standings AS
    SELECT l.league_key, s.* FROM standings s JOIN latest_runs l USING (run_id);

CREATE VIEW current_rosters AS
    SELECT l.league_key, r.*, p.name, p.nfl_team, p.positions_json
    FROM roster_entries r JOIN latest_runs l USING (run_id)
    JOIN players p USING (player_key);

CREATE VIEW current_available_players AS
    SELECT l.league_key, a.*, p.name, p.nfl_team, p.positions_json
    FROM available_players a JOIN latest_runs l USING (run_id)
    JOIN players p USING (player_key);
"""

MIGRATIONS: List[str] = [MIGRATION_1]
SCHEMA_VERSION = len(MIGRATIONS)
