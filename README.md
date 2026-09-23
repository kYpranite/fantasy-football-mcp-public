# Fantasy Football MCP Server

A personal, single-user Model Context Protocol (MCP) server for Yahoo Fantasy Football. It exposes league, roster, matchup, waiver-wire, draft, and lineup-analysis data to AI clients while keeping the underlying fantasy data source separate from the model.

## Current status

This project is built and run as a **single-user app**: you supply your own Yahoo developer credentials and tokens, and the server serves your leagues to your MCP client. It is not intended to be deployed by someone else as a shared, multi-user service.

Experimental hosted, multi-user development lives on the [`multi-user-app` branch](https://github.com/derekrbreese/fantasy-football-mcp-public/tree/multi-user-app). See its [roadmap](https://github.com/derekrbreese/fantasy-football-mcp-public/blob/multi-user-app/docs/MULTI_USER_ROADMAP.md) for planned work toward a possible ChatGPT app. That branch is not a ready-to-deploy shared service; use `main` for personal use.

Note that Yahoo Fantasy Sports API access now requires manual approval from Yahoo, and Yahoo currently provides read access only. Write actions such as adding/dropping players or changing lineups are therefore not part of the tool surface.

## Core capabilities

- Multi-league Yahoo fantasy football discovery
- League settings and standings
- Team rosters and weekly matchups
- Free-agent and waiver-wire research
- Team comparisons
- Draft rankings, recommendations, and draft-state analysis
- Lineup optimization
- Optional external player-context/enrichment integrations

## MCP tools

The main FastMCP server currently exposes:

- `ff_get_leagues`
- `ff_get_league_info`
- `ff_get_standings`
- `ff_get_roster`
- `ff_get_matchup`
- `ff_get_players`
- `ff_compare_teams`
- `ff_build_lineup`
- `ff_get_draft_results`
- `ff_get_waiver_wire`
- `ff_get_draft_rankings`
- `ff_get_draft_recommendation`
- `ff_analyze_draft_state`
- `ff_analyze_reddit_sentiment`
- `ff_get_teams`
- `ff_get_league_settings` — scoring rules, roster slots, waiver/FAAB, trade, playoff settings
- `ff_get_all_rosters` — every team's roster (optional position filter)
- `ff_get_matchups` — all matchups for a week, including past results
- `ff_get_transactions` — adds/drops/trades with FAAB paid, filterable
- `ff_get_faab_history` — every FAAB claim with winning and losing bids, per-manager summary
- `ff_search_players` — who owns a player or whether they are available
- `ff_get_player_history` — a player's roster history, transactions, and draft slot
- `ff_get_sync_status` — freshness of the synced league data

The newer tools (from `ff_get_league_settings` on) read synced league data (`DATA_SOURCE=local`). With `DATA_SOURCE=local`, `ff_get_draft_rankings`, `ff_get_draft_recommendation`, and `ff_analyze_draft_state` report that they need the official API, and `ff_refresh_token` is not applicable. The server also contains maintenance tools used for local operation and troubleshooting.

## Installation

```bash
git clone https://github.com/derekrbreese/fantasy-football-mcp-public.git
cd fantasy-football-mcp-public
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and provide your Yahoo developer credentials. Do not commit `.env`, Yahoo token JSON files, OAuth state, refresh tokens, or other authentication artifacts.

## Yahoo API access

Creating a Yahoo developer application is no longer sufficient by itself to use the Fantasy Sports API. Apply for Fantasy API access through Yahoo's developer access process and associate the approval with your existing client ID.

Yahoo's current access model is read-only. This project therefore treats league-management recommendations separately from transaction execution.

## Browser-backed Yahoo access (in progress)

While Fantasy API approval is pending, league data can be read from the normal Yahoo Fantasy website through your own logged-in browser session (Playwright). You log in manually — Yahoo handles password/2FA/passkeys — and the session is kept in a local browser profile (`.yahoo_browser_profile/`, gitignored). No Yahoo password, cookie, or token is stored in `.env` or source.

First confirm authenticated access (one-time login):

```powershell
pip install -r requirements.txt
python -m playwright install chromium   # only needed if Google Chrome is not installed
python utils/yahoo_browser_login.py --league-id <id>
```

If you sign in to Yahoo with Google (or see "This browser or app may not be secure"), add `--manual-login`: a normal, non-automated Chrome window opens on the same profile; log in, close it, and the script continues with the saved session.

`<id>` is the number in your league URL (`https://football.fantasysports.yahoo.com/f1/<id>`). The script prints the league and team names and writes a sanitized discovery report (JSON endpoints, embedded page state, page HTML with token-like values redacted) to `.yahoo_browser_debug/` (gitignored). To extract the league (metadata, scoring/settings, teams, managers, FAAB, waiver priority, standings, every roster, this week's matchups, available free agents/waiver players, and history — previous weeks' matchups, transactions, FAB offers including losing bids, and draft results):

```powershell
python utils/sync_yahoo_league.py --league-id <id>
python utils/sync_yahoo_league.py --league-id <id> --offense-depth 200   # deeper waiver wire
python utils/sync_yahoo_league.py --league-id <id> --no-players          # skip free agents
python utils/sync_yahoo_league.py --league-id <id> --no-history          # skip history
```

The sync is all-or-nothing for current league state: if any of those pages fails to parse or the result fails validation, nothing is saved. History is best-effort: a failing history part is reported as a warning. Each successful sync is saved to a local SQLite database, `data/league.db` (gitignored; override with `--db` or `LEAGUE_DB_PATH`), in a single transaction, so a failed sync never replaces the previous good data. Every sync is kept as a run, so roster, standings, and FAAB changes can be compared over time; transactions, FAB bids, draft picks, and matchups are merged without duplicates. `python utils/sync_yahoo_league.py --runs` lists recent syncs, and `--json` also writes a debug JSON snapshot. The MCP tools read this database by default (see [Data source](#data-source)). See [docs/BROWSER_EXTRACTION_PLAN.md](docs/BROWSER_EXTRACTION_PLAN.md) for the plan and decision log.

## Data source

`DATA_SOURCE` selects where the MCP tools get league data:

- `local` (default): the SQLite database filled by `utils/sync_yahoo_league.py`. Tools never contact Yahoo; responses include `synced_at`, and `ff_get_sync_status` says when to re-sync. No Yahoo developer credentials are needed.
- `yahoo_api`: the official Yahoo Fantasy Sports API (requires Yahoo's approval and the credentials below).

Run the MCP server with synced data (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python utils\sync_yahoo_league.py --league-id <id>     # refresh data whenever you want
.venv\Scripts\python fantasy_football_multi_league.py                  # stdio MCP server
```

Example Claude Desktop entry (adjust paths):

```json
"fantasy-football": {
  "command": "D:\\path\\to\\fantasy-football-mcp-public\\.venv\\Scripts\\python.exe",
  "args": ["D:\\path\\to\\fantasy-football-mcp-public\\fantasy_football_multi_league.py"],
  "env": {"DATA_SOURCE": "local"}
}
```

## Authentication

Needed only for `DATA_SOURCE=yahoo_api`. The server reads your Yahoo credentials from environment variables:

```env
YAHOO_CLIENT_ID=...
YAHOO_CLIENT_SECRET=...
YAHOO_ACCESS_TOKEN=...
YAHOO_REFRESH_TOKEN=...
YAHOO_GUID=...
```

This single-user mode is the supported way to run the app.

## Running the MCP server

FastMCP HTTP server:

```bash
python fastmcp_server.py
```

By default the server listens on port 8000 locally. Cloud platforms can set `PORT`.

Traditional stdio MCP entry point:

```bash
python fantasy_football_multi_league.py
```

Docker:

```bash
docker build -t fantasy-football-mcp .
docker run --env-file .env -p 8080:8080 fantasy-football-mcp
```

Authentication files and token JSON files are explicitly excluded from the Docker build context.

## Testing

```bash
pytest
```

Credential-isolation tests cover request-scoped token handling and user-namespaced cache keys.

## Security notes

Even as a single-user app, keep credentials out of the repository:

- Never commit Yahoo access or refresh tokens.
- Never bake your tokens into a container image.
- Keep your Yahoo client secret server-side.
- Rotate any credential that has ever been committed to a public Git history.

If a secret was previously committed, deleting the current file is not sufficient by itself: revoke/rotate the credential and, when appropriate, rewrite the repository history.

## Project structure

```text
fantasy-football-mcp-public/
├── fastmcp_server.py
├── fantasy_football_multi_league.py
├── lineup_optimizer.py
├── matchup_analyzer.py
├── position_normalizer.py
├── src/
│   ├── api/
│   │   ├── yahoo_client.py
│   │   └── yahoo_credentials.py
│   ├── datasource/           # local (synced SQLite) data source for MCP tools
│   ├── agents/
│   ├── extractors/
│   │   └── yahoo_web/        # Playwright session, page parsers, league extractor
│   ├── handlers/             # MCP tool handlers (Yahoo API + local_handlers.py)
│   ├── models/
│   ├── services/
│   ├── storage/              # SQLite league store (schema, migrations, repository)
│   └── strategies/
├── tests/
├── utils/
├── Dockerfile
└── requirements.txt
```

## License

See `LICENSE`.
