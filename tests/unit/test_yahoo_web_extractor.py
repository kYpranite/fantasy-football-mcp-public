"""Offline tests for YahooWebExtractor navigation logic using a fake browser page."""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.extractors.yahoo_web.extractor import ExtractionError, YahooWebExtractor  # noqa: E402
from src.extractors.yahoo_web.session import AuthRequired  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "yahoo_web"
FIRST_PAGE = (FIXTURES / "players_O.html").read_text(encoding="utf-8")  # 25 rows, has "Next 25"
LAST_PAGE = (FIXTURES / "players_DEF.html").read_text(encoding="utf-8")  # < 25 rows, no "Next"
EMPTY_WITH_NEXT = '<table><thead><tr><th>Offense</th><th>Roster Status</th></tr></thead><tbody></tbody></table><ul class="pagingnavlist"><li><a href="?count=50">Next 25</a></li></ul>'


class _Response:
    status = 200


class FakePage:
    """Serves HTML per player-list offset (``count=`` query value)."""

    def __init__(self, pages_by_offset, redirect_to_login=False):
        self.pages_by_offset = pages_by_offset
        self.redirect_to_login = redirect_to_login
        self.requested = []
        self.url = ""
        self._html = ""

    def goto(self, url, wait_until=None):
        self.requested.append(url)
        if self.redirect_to_login:
            self.url = "https://login.yahoo.com/?done=x"
            return _Response()
        self.url = url
        offset = int(re.search(r"count=(\d+)", url).group(1))
        self._html = self.pages_by_offset[offset]
        return _Response()

    def content(self):
        return self._html


def extractor(page):
    return YahooWebExtractor(page, "269337", delay_s=0, log=lambda _: None)


def test_scan_follows_next_until_last_page():
    page = FakePage({0: FIRST_PAGE, 25: LAST_PAGE})
    rows, scan = extractor(page)._scan_player_list("O", "S_PSR_2026", limit=500)
    assert [re.search(r"count=(\d+)", u).group(1) for u in page.requested] == ["0", "25"]
    assert scan.pages == 2 and scan.reached_end is True
    assert scan.players == len(rows) == 25 + len(extractor(FakePage({0: LAST_PAGE}))._scan_player_list("DEF", "v", 99)[0])


def test_scan_stops_at_depth_limit_and_says_so():
    page = FakePage({0: FIRST_PAGE, 25: FIRST_PAGE, 50: FIRST_PAGE})
    rows, scan = extractor(page)._scan_player_list("O", "S_PSR_2026", limit=10)
    assert len(page.requested) == 1
    assert len(rows) == scan.players == 10
    assert scan.reached_end is False


def test_scan_skips_players_repeated_across_pages():
    # Same page served twice (list shifted between loads): duplicates dropped, not double-counted.
    page = FakePage({0: FIRST_PAGE, 25: FIRST_PAGE, 50: LAST_PAGE})
    rows, scan = extractor(page)._scan_player_list("O", "S_PSR_2026", limit=500)
    ids = [p.player_id for p, _ in rows]
    assert len(ids) == len(set(ids))
    assert scan.pages == 3


def test_scan_fails_on_empty_page_mid_pagination():
    page = FakePage({0: FIRST_PAGE, 25: EMPTY_WITH_NEXT})
    with pytest.raises(ExtractionError, match="empty page at offset 25"):
        extractor(page)._scan_player_list("O", "S_PSR_2026", limit=500)


def test_scan_fails_when_first_page_empty():
    page = FakePage({0: EMPTY_WITH_NEXT})
    with pytest.raises(ExtractionError, match="first page has no players"):
        extractor(page)._scan_player_list("O", "S_PSR_2026", limit=500)


def test_fetch_detects_expired_login():
    with pytest.raises(AuthRequired, match="--manual-login"):
        extractor(FakePage({}, redirect_to_login=True)).fetch("home", "players?count=0")
