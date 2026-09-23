"""Data sources behind the MCP tools (local synced store vs. official Yahoo API)."""

import os

LOCAL = "local"
YAHOO_API = "yahoo_api"


def data_source_mode() -> str:
    """``DATA_SOURCE`` env: ``local`` (default, synced SQLite store) or ``yahoo_api``."""
    mode = (os.environ.get("DATA_SOURCE") or LOCAL).strip().lower()
    return mode if mode in (LOCAL, YAHOO_API) else LOCAL
