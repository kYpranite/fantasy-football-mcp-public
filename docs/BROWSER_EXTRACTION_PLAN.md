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
Local storage              (format TBD — see Open decisions)
   ▼
Data source layer          LeagueDataSource protocol: local store | official Yahoo API
   ▼
Existing MCP handlers      src/handlers/*  → fastmcp_server.py ff_* tools
   ▼
LLM
```

- Extraction is a **producer** run manually (`utils/sync_yahoo_league.py`, planned);
  the MCP reads only the local store.
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
| 2026-09-22 | Storage format **undecided** — SQLite explicitly not chosen yet | To be discussed before Milestone 6 |

## Security rules

- Never store Yahoo password, cookies, OAuth tokens, or Authorization headers in code,
  `.env`, fixtures, or logs. Never expose them to the LLM.
- Gitignored: `.env*`, `.py.json`, `.yahoo_browser_profile/`, `.yahoo_browser_debug/`,
  `/data/` (local league data).
- Debug output is scrubbed (`scrub_text`: crumb/token/session/auth/cookie values,
  emails) and URLs are redacted (`redact_url`: query values removed).
- Committed fixtures must be trimmed and pseudonymized (team/manager names).
- `utils/setup_yahoo_auth.py` still writes tokens to `.py.json` (now gitignored);
  exposed Yahoo client secret should be rotated before the official API is used.
- Do not bypass Yahoo login, CAPTCHA, 2FA, or other access controls.

## Milestones

| # | Scope | Status |
|---|---|---|
| 1 | Inspect repo, propose architecture | ✅ Done |
| 2 | Playwright persistent session; read league name + team names | ✅ Done — commit `4048dad` |
| 3 | League metadata, settings/scoring, teams, all current rosters, standings → normalized models (+ offline fixture tests) | ⏳ Next |
| 4 | Current matchups, free agents/waivers (with pagination), FAAB/waiver priority | Planned |
| 5 | Transactions, historical matchups, draft results | Planned |
| 6 | Local persistence (format TBD), snapshots/history, validation, staged commit so failed syncs never replace good data | Planned — storage decision pending |
| 7 | `LeagueDataSource` + local implementation wired into existing handlers/tools; new tools (league settings, all rosters, transactions, sync status) | Planned |
| 8 | `DATA_SOURCE` switch; `YahooApiSource` built from existing API code | Planned |

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

## Open decisions / questions

- Local storage format and history model (snapshots vs. change log).
- Whether fixtures keep real team names (repo visibility).
- Which additional pages hold settings/scoring, rosters, FAAB, transactions, draft
  results — to be confirmed by capturing pages in Milestone 3+.
- Yahoo ToS discourages automated access: keep syncs manual, own league only,
  throttled (1–3 s between page loads).
