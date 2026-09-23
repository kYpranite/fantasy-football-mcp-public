# Browser-Backed Yahoo Data Source — Plan & Decision Log

Living document for turning this MCP into an "AI fantasy co-manager" that reads the
whole league from the Yahoo Fantasy **website** (via the user's own logged-in browser)
instead of the Yahoo Fantasy Sports API, which returns HTTP 403 until Yahoo approves
the developer app. Update this file whenever a milestone lands or a decision changes.

Branch: `feature/browser-extraction`

## Goal

Let an LLM answer league-management questions (start/sit, pickups, drops, FAAB,
trades, opponent weaknesses, manager tendencies, playoff outlook, history) by calling
focused MCP tools — not one giant league dump — backed by league data synced from
Yahoo on demand, not scraped per request.

## Architecture (target)

```
Yahoo Fantasy website
   │  (user's authenticated browser session, Playwright)
   ▼
Yahoo web extractor        src/extractors/yahoo_web/   — pages, parsers (HTML → models)
   ▼
Normalized data model      src/models/                 — Yahoo-API-style keys
   ▼
Local storage              src/storage/  — SQLite (data/league.db)
   ▼
Data source layer          LeagueDataSource protocol: local store | official Yahoo API
   ▼
Existing MCP handlers      src/handlers/*  → fastmcp_server.py ff_* tools
   ▼
LLM
```

- Extraction is a **producer** run manually (`utils/sync_yahoo_league.py`) that writes to
  SQLite; the MCP reads only the local store.
- `DATA_SOURCE=local|yahoo_api` selects the implementation behind the handlers, so the
  official API can be restored if Yahoo approves the app.
- Yahoo page structure is isolated in the extractor; MCP tools never see selectors.

## Current codebase (findings from Milestone 1)

- `fastmcp_server.py` — FastMCP tools (`ff_*`), prompts, resources. Each tool calls
  `_call_legacy_tool` → `fantasy_football_multi_league.call_tool` → `TOOL_HANDLERS`.
- `src/handlers/*_handlers.py` — tool logic. Dependencies injected by
  `inject_*_dependencies()` at the bottom of `fantasy_football_multi_league.py`.
- `src/api/yahoo_client.yahoo_api_call(endpoint)` — only Yahoo HTTP client.
  Handlers pass raw Yahoo endpoint strings and parse Yahoo's nested JSON inline —
  this is the coupling the data-source layer must remove.
- Yahoo call sites: `fantasy_football_multi_league.py` (`discover_leagues`,
  `get_user_team_info`, `get_all_teams_info`, `get_waiver_wire_players`,
  `get_draft_rankings`); `league_handlers.py`, `roster_handlers.py`,
  `matchup_handlers.py`, `player_handlers.py`.
- Reusable without Yahoo API: Sleeper enrichment (`sleeper_api.py`,
  `lineup_optimizer.enhance_with_external_data`), strategies, optimizer, Reddit,
  bye-week utilities. External NFL enrichment should build on these later.
- No existing tools for transactions, league settings/scoring, or all rosters;
  `ff_get_teams` exists in the legacy layer but is not exposed in `fastmcp_server.py`.
- Root `lineup_optimizer.py` / `sleeper_api.py` / `yahoo_api_utils.py` duplicate
  diverged copies in `src/`; handlers import the root ones. Left alone.

## Decisions

| Date | Decision | Why |
|---|---|---|
| 2026-09-22 | Playwright with a persistent, gitignored profile (`.yahoo_browser_profile/`), installed Chrome first, bundled Chromium fallback | User logs in manually; no credentials/cookies in code or `.env` |
| 2026-09-22 | `--manual-login`: log in through a plain, non-automated Chrome window on the same profile, then automate with the saved session | Google sign-in rejects automation-controlled browsers ("This browser or app may not be secure"); we do not try to hide automation |
| 2026-09-22 | Parse Yahoo's **server-rendered HTML**; passively record JSON the pages load themselves; **never call `pub-api*.fantasysports.yahoo.com` directly** | League pages carry data in HTML. The web app's `pub-api` host is the same Fantasy API that is gated for unapproved apps — calling it directly would sidestep that access control |
| 2026-09-22 | Store Yahoo-API-style IDs (`nfl.l.<league>`, `nfl.l.<league>.t.<n>`, `nfl.p.<player>`) | Seamless switch between web and API sources |
| 2026-09-22 | `tests/` stays gitignored; new tests/fixtures are committed with `git add -f` | Keep upstream's convention |
| 2026-09-22 | HTML parsing with BeautifulSoup + lxml; columns located by header label, never by position | Own-team and other-team roster pages have different column layouts |
| 2026-09-22 | Open roster slots derived from settings (`Roster Positions`) minus occupied slots, not from "(Empty)" rows | Yahoo shows empty rows only on your own team, and inconsistently |
| 2026-09-22 | Until storage is decided, `sync_yahoo_league.py` writes a validated snapshot as JSON to `.yahoo_browser_debug/snapshots/` (debug only) | Lets us inspect real output without committing to a storage design |
| 2026-09-22 | Available players: status `A` (free agents + waivers), groups O/K/DEF, three views merged per player (rest-of-season proj, week proj, season total), sorted by points, depth-limited (O 100, K 25, DEF 50 per view) | Deep waiver-wire players are irrelevant; the limit is recorded per scan (`player_scans`) and surfaced as a warning so it is never mistaken for the full pool |
| 2026-09-22 | History (past matchups, transactions, FAB offers, draft) never blocks a sync: each failing part becomes a warning; history checks are warnings, not errors | Requirement: history must not block current-state extraction |
| 2026-09-22 | Join players on `player_key` only, never on names | Yahoo labels the same DEF "Chiefs" or "Kansas City" between page loads |
| 2026-09-22 | MCP: `DATA_SOURCE=local` (default) swaps handlers in `call_tool` for `local_handlers`; `yahoo_api` keeps the original handlers untouched. Roster enrichment and lineup optimization are shared functions both use | Same tool names/arguments for the LLM; no duplicated Sleeper/optimizer logic |
| 2026-09-22 | Storage: **SQLite** (`src/storage/`, `data/league.db`), chosen over DuckDB/Postgres | Stdlib, single file; one transaction per sync so failures never replace good data; WAL lets the MCP read while a sync writes. DuckDB is analytics-oriented and awkward with concurrent processes; Postgres needs a server |

## Security rules

- Never store Yahoo password, cookies, OAuth tokens, or Authorization headers in code,
  `.env`, fixtures, or logs. Never expose them to the LLM.
- Gitignored: `.env*`, `.py.json`, `.yahoo_browser_profile/`, `.yahoo_browser_debug/`,
  `/data/` (local league data, incl. `data/league.db` with manager names).
- Debug output is scrubbed (`scrub_text`: crumb/token/session/auth/cookie values,
  emails) and URLs are redacted (`redact_url`: query values removed).
- Committed fixtures must be trimmed and pseudonymized (team/manager names) — done by `tests/fixtures/yahoo_web/build_fixtures.py`, which refuses to write a fixture if a real name remains.
- `utils/setup_yahoo_auth.py` still writes tokens to `.py.json` (now gitignored);
  exposed Yahoo client secret should be rotated before the official API is used.
- Do not bypass Yahoo login, CAPTCHA, 2FA, or other access controls.

## Milestones

| # | Scope | Status |
|---|---|---|
| 1 | Inspect repo, propose architecture | ✅ Done |
| 2 | Playwright persistent session; read league name + team names | ✅ Done — commit `4048dad` |
| 3 | League metadata, settings/scoring, teams, all current rosters, standings → normalized models (+ offline fixture tests) | ✅ Done — `utils/sync_yahoo_league.py` |
| 4 | Current matchups, free agents/waivers (with pagination) | ✅ Done (FAAB/waiver priority done in 3; add/drop trends deferred) |
| 5 | Transactions, historical matchups, draft results | ✅ Done (+ FAB offers with losing bids) |
| 6 | Local persistence, snapshots/history, validation, staged commit so failed syncs never replace good data | ✅ Done — SQLite `data/league.db` |
| 7 | Local data source wired into existing handlers/tools; new tools (league settings, all rosters, transactions, FAAB, search, sync status) | ✅ Done |
| 8 | `DATA_SOURCE` switch; `YahooApiSource` built from existing API code | ◐ Switch done in 7 (`yahoo_api` = original handlers); API-backed sync into the same store still planned |

## Milestone 2 results — what Yahoo's league page looks like

`python utils/yahoo_browser_login.py --league-id <id> [--manual-login]`

- Session reuse works: second run reached the league page with no login.
- `/f1/<league_id>` is server-rendered HTML (~3 MB). Useful anchors:
  `#standingstable`, `#leaguestandings`, `#matchupweek`, `#scoreboard`,
  a transactions table (`.Tst-transaction-table`), `playernote-<player_id>` ids.
- Team pages link as `/f1/<league_id>/<team_id>`.
- An inline ad-targeting script (`SmartAd`) includes current team id/name/rank, this
  week's opponent, scores, current roster player ids, and a team-id→name map.
  Useful as a cross-check only — ad code can change without notice.
- JSON network traffic on the page is ads/telemetry plus a `pub-api-ro` profile call;
  no league data arrives as JSON on this page.
- Known bug: the logged-in user's team is labelled "My Team" (nav link text);
  Milestone 3 takes names from the standings table instead.

## Milestone 3 results — core league extraction

`python utils/sync_yahoo_league.py --league-id <id> [--save-pages] [--headless] [--delay 2]`

13 page loads (~30 s at 2 s spacing): league home, settings, managers, one page per team.
All-or-nothing: any parse failure or validation error aborts with nothing saved.

| Page | URL | Parser | Provides |
|---|---|---|---|
| League home | `/f1/<id>` | `parse_league_home` | `#standingstable` (rank, W-L-T, PF, PA, streak; `tr.Selected` = your team), season (`#seasonspec`), current week (`#matchup_selectlist_nav .flyout-title`) |
| Settings | `/f1/<id>/settings` | `parse_settings` | `#settings-table` (every label/value kept in `raw`), `#settings-stat-mod-table` (scoring rules, league vs Yahoo default) |
| Managers | `/f1/<id>/teams` | `parse_managers` | manager display names, commissioner flag, FAAB remaining, waiver priority, moves, trades, last activity |
| Team | `/f1/<id>/<team_id>` | `parse_team_roster` | `#statTable0..2` (offense, K, DEF): slot, player id/name, NFL team, positions, injury status, bye, points, projection, % started, % rostered, next game |

Modules: `src/models/league_data.py` (models + key helpers),
`src/extractors/yahoo_web/parsers.py` (pure HTML parsers), `extractor.py` (navigation,
throttling, `validate_snapshot`), `utils/sync_yahoo_league.py` (CLI).

Validation errors (abort): <2 teams, duplicate teams, no league name, no current week,
not exactly one "my team", standings/team mismatch, missing or empty roster, player on
two rosters, more players in a slot than the league allows.
Warnings (kept on snapshot): missing manager details; settings `Max Teams` ≠ actual teams
(this league: 12 vs 10 — validation uses the actual count).

Offline tests: `tests/unit/test_yahoo_web_parsers.py` against sanitized fixtures in
`tests/fixtures/yahoo_web/` (built by `build_fixtures.py`, which trims pages to parsed
elements and replaces league/team/manager names with placeholders).

Observations for later milestones:
- `/f1/<id>/standings` is actually **live matchup scoring** (React markup) — use in Milestone 4.
- Home page also has a transactions table (`.Tst-transaction-table`) — Milestone 5.
- Some teams hold Q-status players in IR slots — possible "illegal IR" insight later.
- Pre-existing test env gaps (not from this work): `aiohttp`, `mcp`, `pytest-asyncio`
  not installed in the system Python, so 8 legacy test modules fail to import/run.

## Milestone 4 results — matchups and available players

`python utils/sync_yahoo_league.py --league-id <id> [--offense-depth 100] [--no-players]`

A full sync is ~43 page loads (~2.5 min at 2 s spacing): 13 core pages + 30 player-list pages.

| Data | Source | Parser | Notes |
|---|---|---|---|
| Current-week matchups | league home `#matchupweek` (already fetched) | `parse_matchups` | both teams, points, projected points, week status ("Not started yet"…) |
| Available players | `/f1/<id>/players?status=A&pos={O,K,DEF}&stat1=<view>&sort=PTS&sdir=1&count=<offset>` | `parse_player_list` | 25 rows/page, "Next 25" link; free agent vs waivers + waiver clear date ("W (Sep 23)"), injury, bye, GP, % rostered, preseason/current rank; the "Fan Pts" column is the view's value |

Views merged into `AvailablePlayer`: `S_PSR_<season>` → `projected_rest_of_season` (sets
order), `S_PW_<week>` → `projected_week`, `S_S_<season>` → `season_points`.

Pagination safeguards (`YahooWebExtractor._scan_player_list`): stops at the last page or
the depth limit; repeated players across pages are dropped; an empty first page or an
empty page that still links "Next" aborts the sync. Validation adds: matchup teams must be
known and appear once; matchup week = current week; players both rostered and available
are flagged (a transaction mid-sync can cause it).

Offline tests: `tests/unit/test_yahoo_web_extractor.py` (pagination with a fake page,
expired-login detection) plus matchup/player-list cases in `test_yahoo_web_parsers.py`.

Deferred / found for later:
- Add/drop trends: the Research view (`stat1=R_O`) has % rostered/% started deltas, average
  draft pick, depth-chart role, opponent rank — sort order unclear; not collected yet.
- Single matchup page (`/f1/<id>/matchup?week=N&mid1=A&mid2=B`) has win probability.
- Other weeks' matchups: `?matchup_week=N&module=matchups` on league home (Milestone 5).

## Milestone 5 results — history

Collected by default (`--no-history` skips it). Current full sync: ~50 page loads, ~2 min 50 s.

| Data | URL | Parser | Notes |
|---|---|---|---|
| Past weeks' matchups | `/f1/<id>/?matchup_week=N&module=matchups&lhst=matchups` (weeks 1..current-1) | `parse_matchups` (same as current week) | "Final results"; `Matchup.winner_team_key` derived from points once final |
| Transactions | `/f1/<id>/transactions?transactionsfilter=all&count=<offset>` (25/page, all pages) | `parse_transactions` | per player: action (add/drop/trade), Yahoo detail ("Free Agent", "$18 Waiver", "To Waivers"), FAAB bid; acting team; timestamp |
| FAB offers | `/f1/<id>/transactions?transactionsfilter=faab&count=<offset>` | `parse_waiver_claims` | every processed claim: winning bid, awarded team, **every losing bid with team, amount, reason** ("Lower Offer", "Lower waiver priority") |
| Draft | `/f1/<id>/draftresults` | `parse_draft_results` | 15 "Round N" tables; teams shown by name only → mapped to team keys (unmatched → `team_key=None` + warning) |

Details:
- Timestamps have no year ("Sep 22, 6:04 pm"): Jul–Dec → season year, Jan–Jun → next year.
  Stored as local ISO time plus the raw label. Time zone is whatever Yahoo displays (EDT here).
- Transactions have no Yahoo id; `transaction_id` is a hash of team, time, and players, and
  duplicates across pages are dropped.
- Team defenses link to `/nfl/teams/<slug>/`, not player pages. The id comes from
  `data-ys-playerid` when present, otherwise 100000 + Yahoo NFL team id (table in
  `parsers._YAHOO_NFL_TEAM_IDS`, verified against ids seen on roster/player pages).
- Paged history lists stop at 40 pages (1,000 rows) with a warning if hit.
- No trades yet in this league, so trade rows are parsed generically (icon title +
  detail text) and untested against a real trade.
- `/f1/<id>/scoreboard` does not exist (404 page).
- Scrubbing also redacts URL-encoded emails (`%40`) found in Yahoo's account menu.

## Milestone 6 results — SQLite store

`src/storage/`: `schema.py` (migrations; version in `PRAGMA user_version`), `db.py`
(`connect()`: WAL, foreign keys, busy timeout, auto-migrate; `LEAGUE_DB_PATH` or
`data/league.db`), `repository.py` (`LeagueStore`).

| Kind | Tables | Behavior |
|---|---|---|
| Runs | `sync_runs` | one row per attempt: `committed` or `failed` (+ error), warnings, what was included |
| Per-run snapshot | `league_state` (settings JSON, player scans), `scoring_rules`, `teams`, `standings`, `rosters` (open slots), `roster_entries`, `available_players` | full set per committed run → history of rosters/standings/FAAB |
| Merged history | `players`, `matchups`, `transactions` + `transaction_players`, `waiver_claims` + `waiver_bids`, `draft_picks`, `leagues` | upserted by natural key; re-syncs never duplicate |
| Views | `latest_runs`, `current_teams`, `current_standings`, `current_rosters`, `current_available_players` | "current" = latest committed run per league |

- `save_snapshot` writes a whole sync in one `BEGIN IMMEDIATE … COMMIT`; any error rolls
  back everything (tested: duplicate roster player → IntegrityError → previous run stays current).
- `record_failed_sync` logs auth/extraction failures without touching league data.
- `load_snapshot(league_key, run_id=None)` rebuilds a `LeagueSnapshot` (latest by default).
- `player_roster_history(player_key)` → team/slot per run (who changed teams, when).
- `players` keeps one name per player (roster/available lists win over history pages,
  because DEF labels vary); draft picks keep NFL team/position as of the draft.
- First live run: ~240 KB for 10 teams, 155 rostered, 184 available, 41 transactions,
  150 draft picks, 342 known players.

CLI: `sync_yahoo_league.py` saves to the DB by default; `--db PATH`, `--runs` (list syncs),
`--json` (extra debug JSON). Tests: `tests/unit/test_league_store.py`.

## Milestone 7 results — MCP tools on local data

- `src/datasource/local_source.py` — `LocalLeagueSource`: JSON-ready queries over the store
  (caches the latest run's snapshot; reloads automatically after a new sync). Accepts
  `nfl.l.<id>`, `461.l.<id>`, or bare ids; team keys or bare team ids.
- `src/handlers/local_handlers.py` — handlers for all 18 existing tools + 8 new ones
  (`LOCAL_TOOL_SPECS`). Missing data returns an error with the sync command.
- Shared with the API path: `roster_handlers.enhance_roster_result` (Sleeper projections,
  tiers, bye weeks), `matchup_handlers.build_lineup_from_roster` (lineup optimizer),
  `player_handlers.handle_ff_get_waiver_wire` (injected local player list).
- `fantasy_football_multi_league.py`: `DATA_SOURCE` switch; `list_tools` includes the new
  tools; stdio `main()` routes stray `print()` output to stderr (verified: `ff_build_lineup`
  over stdio returns valid JSON-RPC).
- `fastmcp_server.py`: wrappers for the new tools + `ff_get_teams`; instructions mention
  `synced_at`/freshness and requesting only needed data.
- Verified end to end with the FastMCP in-memory client against the real league DB.
- Tests: `tests/unit/test_local_source.py` (+ shared `league_fixtures.py`). Full suite in
  `.venv`: 220 passed, 5 failed — the 5 are pre-existing failures in
  `tests/unit/test_api_client.py` (identical on the pre-Milestone-7 commit).

Known limitations / follow-ups:
- The lineup optimizer (legacy) can recommend a Doubtful player with a Yahoo projection
  of 0 when Sleeper projects more — consider weighting injury status.
- `ff_get_waiver_wire` "trending" sort has no add/drop trend data yet (Research view).
- Draft-prep tools (rankings/ADP/recommendations) need the official API.

## Open decisions / questions

- Which additional pages hold settings/scoring, rosters, FAAB, transactions, draft
  results — to be confirmed by capturing pages in Milestone 3+.
- Yahoo ToS discourages automated access: keep syncs manual, own league only,
  throttled (1–3 s between page loads).
