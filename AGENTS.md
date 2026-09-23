# Agent Guidelines

Rules for coding agents working in this repository. Current plan, decisions, and
milestone status: [docs/BROWSER_EXTRACTION_PLAN.md](docs/BROWSER_EXTRACTION_PLAN.md).

## Commits

- Commit as you go: group changes into small, logical commits (one coherent change
  each — e.g. models, then parsers + the docs they affect, then tests/fixtures), not
  one large commit per milestone.
- Commit message subject lines must be under 100 characters; keep messages to 1–2 lines.
- Code/behavior changes update affected docs (README, INSTALLATION, plan doc) in the
  same commit.
- Work on a feature branch, not `main`.

## Tests

- `tests/` and `test_*.py` are gitignored upstream. Commit new tests and fixtures with
  `git add -f <path>`; never un-ignore `tests/` without asking.
- Parsers must be testable offline against sanitized fixtures in
  `tests/fixtures/yahoo_web/` (regenerate with `build_fixtures.py`; never commit real
  league/team/manager names, emails, cookies, or tokens).

## Security

- Never store or print Yahoo passwords, cookies, OAuth tokens, client secrets, or
  Authorization headers; never add them to code, `.env.example`, fixtures, or logs.
- Keep gitignored: `.env*`, `.py.json`, `.yahoo_browser_profile/`,
  `.yahoo_browser_debug/`, `/data/`.
- Do not bypass Yahoo login, CAPTCHA, 2FA, or other access controls. Do not hide
  browser automation.
- Browser extraction parses Yahoo's web pages; never call
  `pub-api*.fantasysports.yahoo.com` directly.

## Architecture

- Yahoo page structure lives only in `src/extractors/yahoo_web/parsers.py`; MCP tools
  and handlers must not depend on selectors.
- Synced league data is stored in SQLite (`src/storage/`, default `data/league.db`).
  Schema changes go in a NEW migration appended to `MIGRATIONS` in
  `src/storage/schema.py`; never edit an applied migration.
- A sync is written in one transaction (`LeagueStore.save_snapshot`); a failed sync
  must never modify previously committed league data.
- Commands are run on Windows PowerShell; use `curl.exe`, not `curl`.
