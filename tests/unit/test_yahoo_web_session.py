"""Offline tests for Yahoo web session helpers (no browser, no Yahoo session)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.extractors.yahoo_web.session import (  # noqa: E402
    REDACTED,
    extract_team_links,
    is_auth_url,
    is_fantasy_url,
    is_sensitive_key,
    league_url,
    redact_url,
    scrub_text,
)
from utils.yahoo_browser_login import summarize_shape  # noqa: E402


def test_league_url():
    assert league_url("269337") == "https://football.fantasysports.yahoo.com/f1/269337"


def test_auth_and_fantasy_url_detection():
    assert is_auth_url("https://login.yahoo.com/?done=https%3A%2F%2Ffootball.fantasysports.yahoo.com")
    assert is_auth_url("https://guce.yahoo.com/consent")
    assert not is_auth_url("https://football.fantasysports.yahoo.com/f1/269337")
    assert is_fantasy_url("https://football.fantasysports.yahoo.com/f1/269337")
    assert not is_fantasy_url("https://evil.example/football.fantasysports.yahoo.com")


def test_redact_url_keeps_keys_drops_values_and_fragment():
    url = "https://football.fantasysports.yahoo.com/f1/269337/players?status=A&pos=RB&crumb=abc123#top"
    redacted = redact_url(url)
    assert "abc123" not in redacted
    assert "RB" not in redacted
    assert "status=" in redacted and "crumb=" in redacted
    assert "#top" not in redacted
    assert redacted.startswith("https://football.fantasysports.yahoo.com/f1/269337/players?")


def test_scrub_text_redacts_json_and_query_secrets():
    text = (
        '{"crumb":"s3cr3t","accessToken":"tok\\"en","league_name":"My League"}'
        ' <a href="/f1/269337?crumb=xyz&week=3">'
    )
    scrubbed = scrub_text(text)
    assert "s3cr3t" not in scrubbed
    assert "tok" not in scrubbed
    assert "xyz" not in scrubbed
    assert '"league_name":"My League"' in scrubbed
    assert "week=3" in scrubbed
    assert scrubbed.count(REDACTED) == 3


def test_sensitive_key_does_not_hit_fantasy_keys():
    assert is_sensitive_key("crumb")
    assert is_sensitive_key("session_id")
    assert not is_sensitive_key("league_key")
    assert not is_sensitive_key("player_key")
    assert not is_sensitive_key("team_name")


def test_extract_team_links_dedupes_and_sorts():
    base = "https://football.fantasysports.yahoo.com"
    links = [
        (f"{base}/f1/269337/10", "Team Ten"),
        (f"{base}/f1/269337/2", "  Team\n Two "),
        (f"{base}/f1/269337/2", "Team Two duplicate"),
        (f"{base}/f1/269337/3", ""),  # logo link with no text
        (f"{base}/f1/269337/3/", "Team Three"),
        (f"{base}/f1/269337/players", "Players"),
        (f"{base}/f1/269337/2/team?week=3", "Weekly view"),
        (f"{base}/f1/999999/4", "Other league"),
    ]
    assert extract_team_links(links, "269337") == [
        ("2", "Team Two"),
        ("3", "Team Three"),
        ("10", "Team Ten"),
    ]


def test_summarize_shape_has_no_values():
    shape = summarize_shape({"league": {"name": "Secret Name", "crumb": "x"}, "teams": [{"id": 1}]})
    assert shape == {
        "league": {"name": "str", "crumb": REDACTED},
        "teams": ["array(1)", {"id": "int"}],
    }


def test_scrub_text_redacts_email():
    assert scrub_text('{"email":"someone.x+ff@example.co.uk"}') == '{"email":"REDACTED"}'


def test_is_fantasy_related_skips_ads():
    from utils.yahoo_browser_login import is_fantasy_related

    assert is_fantasy_related("https://football.fantasysports.yahoo.com/f1/269337/players")
    assert is_fantasy_related("https://sports.yahoo.com/site/api/resource/fantasy/profile")
    assert not is_fantasy_related("https://pbs.yahoo.com/openrtb2/auction")
    assert not is_fantasy_related("https://ads.example.com/fantasy")
