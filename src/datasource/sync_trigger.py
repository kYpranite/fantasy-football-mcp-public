"""Start a league sync in the background (for the ff_sync_league MCP tool) and track it.

A sync takes minutes — longer than MCP clients wait for a tool — so the tool starts
``utils/sync_yahoo_league.py`` as a detached process and returns immediately; progress
and results show up in ``ff_get_sync_status``. A lock file prevents overlapping syncs
(tool-started or manual), and a minimum interval avoids hammering Yahoo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from src.storage.db import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "data"
LOCK_PATH = DATA_DIR / "sync.lock"
LOG_PATH = DATA_DIR / "sync.log"
SYNC_SCRIPT = PROJECT_ROOT / "utils" / "sync_yahoo_league.py"
MIN_INTERVAL_S = 10 * 60  # refuse a new sync this soon after the last successful one (unless forced)
MIN_RETRY_S = 2 * 60  # ...or this soon after any attempt, including failed ones
LOCK_STALE_S = 20 * 60  # a lock older than this is from a crashed sync
MODES = {
    "quick": ["--no-history"],  # rosters, standings, matchups, waivers (~1.5 min)
    "full": [],  # + transactions, FAB offers, draft, past weeks (~3 min)
}


class SyncInProgress(RuntimeError):
    """Another sync holds the lock."""


def read_lock(lock_path: Path = LOCK_PATH) -> Optional[Dict[str, Any]]:
    """Active lock info, or None if there is no lock or it is stale."""
    try:
        info = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - float(info.get("started_at", 0)) > LOCK_STALE_S:
        return None
    return info


TOKEN_ENV = "FF_SYNC_LOCK_TOKEN"


@contextmanager
def sync_lock(league_id: str, mode: str, lock_path: Path = LOCK_PATH) -> Iterator[None]:
    """Held by the sync script for the whole run.

    A lock written by ``start_background_sync`` for this very process carries a token
    passed via the environment (PIDs are unreliable: the venv launcher re-spawns Python).
    """
    token = os.environ.get(TOKEN_ENV) or uuid.uuid4().hex
    current = read_lock(lock_path)
    if current and current.get("token") != token:
        raise SyncInProgress(f"A sync started at {time.ctime(current['started_at'])} is still running")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"token": token, "pid": os.getpid(), "started_at": time.time(),
                    "league_id": league_id, "mode": mode}),
        encoding="utf-8",
    )
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def start_background_sync(
    league_id: str,
    mode: str = "full",
    force: bool = False,
    last_synced_epoch: Optional[float] = None,
    last_attempt_epoch: Optional[float] = None,
    lock_path: Path = LOCK_PATH,
    log_path: Path = LOG_PATH,
    popen=subprocess.Popen,
) -> Dict[str, Any]:
    if mode not in MODES:
        return {"status": "error", "error": f"mode must be one of {sorted(MODES)}"}
    running = read_lock(lock_path)
    if running:
        return {"status": "already_running", "started": time.ctime(running["started_at"]),
                "mode": running.get("mode"), "message": "Check ff_get_sync_status for completion."}
    if not force and last_attempt_epoch and time.time() - last_attempt_epoch < MIN_RETRY_S:
        return {"status": "recently_attempted",
                "message": "A sync was attempted under 2 minutes ago; check ff_get_sync_status "
                           "(pass force=true to retry now)."}
    if not force and last_synced_epoch and time.time() - last_synced_epoch < MIN_INTERVAL_S:
        minutes = round((time.time() - last_synced_epoch) / 60, 1)
        return {"status": "recently_synced", "minutes_since_last_sync": minutes,
                "message": f"Last sync was {minutes} min ago; pass force=true to sync anyway."}

    token = uuid.uuid4().hex
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Lock first (with the token the child will present) so concurrent calls can't double-start.
    lock_path.write_text(
        json.dumps({"token": token, "pid": None, "started_at": time.time(),
                    "league_id": str(league_id), "mode": mode}),
        encoding="utf-8",
    )
    log = open(log_path, "w", encoding="utf-8")
    flags = 0
    if os.name == "nt":  # detach from the MCP server's console/process group
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        process = popen(
            [sys.executable, str(SYNC_SCRIPT), "--league-id", str(league_id), *MODES[mode]],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, TOKEN_ENV: token},
            stdin=subprocess.DEVNULL,
            stdout=log,  # never the MCP server's stdout (it carries the protocol)
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
        )
    except Exception:
        lock_path.unlink(missing_ok=True)
        raise
    finally:
        log.close()
    return {
        "status": "started", "mode": mode, "pid": process.pid,
        "expected_minutes": 1.5 if mode == "quick" else 3,
        "message": "Sync running in the background (a Chrome window may open). "
                   "Call ff_get_sync_status to see when it finishes; data updates automatically.",
    }


def log_tail(lines: int = 8, log_path: Path = LOG_PATH) -> Optional[str]:
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return "\n".join(content[-lines:])
