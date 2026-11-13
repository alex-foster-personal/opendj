"""Fixture real-content gate: refuses publish-gate leak classes in tests (issue #11397).

[if] a tracked fixture carries a publish-gate real-content class [then] the audit names it, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.fixture_real_content_rules import (
    REAL_LIBRARY_STABLE_IDS,
    fixture_real_content_findings,
)
from scripts.oss_tip_audit import audit_fixture_real_content
from tests.lyrics.lyrics_asr_fixtures import synthetic_stable_id

pytestmark = pytest.mark.requirement("OSSPUB-14866")

REPO_ROOT = Path(__file__).resolve().parents[2]

_USERS = "/" + "Users" + "/"
_LOGIN = "fixture" + "-owner"
_REAL_HOME = _USERS + _LOGIN + "/Music/track.flac"
_STROBE = "St" + "robe"
_ARTIST = "dead" + "mau5"


@pytest.fixture(scope="module")
def tracked_fixture_findings() -> tuple[str, ...]:
    return tuple(f.render() for f in audit_fixture_real_content(REPO_ROOT))


def test_the_tracked_tree_has_no_fixture_real_content_leaks(tracked_fixture_findings: tuple[str, ...]) -> None:
    """[if] tracked fixtures match a publish real-content class [then] fail, [else stop]."""
    assert tracked_fixture_findings == ()


def test_planted_lyrics_asr_cache_with_library_stable_id_is_refused(tmp_path: Path) -> None:
    """[if] lyrics_asr carries a real library stable_id [then] gate fails, [else stop]."""
    stable_id = next(iter(REAL_LIBRARY_STABLE_IDS))
    rel = "tests/fixtures/lyrics_asr/planted_cache.json"
    text = json.dumps({"stable_id": stable_id, "lines": [{"text": "invented hook"}]})
    findings = fixture_real_content_findings(rel, text)
    assert any(f.rule == "fixture-lyrics-stable-id" for f in findings)


def test_planted_lyrics_asr_copyright_fragment_is_refused(tmp_path: Path) -> None:
    """[if] lyrics_asr lines carry a captured song fragment [then] fail, [else stop]."""
    rel = "tests/fixtures/lyrics_asr/planted_cache.json"
    text = json.dumps(
        {
            "stable_id": synthetic_stable_id("planted_cache.json"),
            "lines": [{"text": "I'm losing it"}],
        }
    )
    findings = fixture_real_content_findings(rel, text)
    assert any(f.rule == "fixture-lyrics-copyright" for f in findings)


def test_planted_anlz_manifest_commercial_selection_is_refused() -> None:
    """[if] an ANLZ manifest selection names a commercial title [then] fail, [else stop]."""
    rel = "apps/webui/frontend/tests/unit/fixtures/planted.manifest.json"
    text = json.dumps(
        {
            "capture": {
                "selection": f"{_STROBE} ({_ARTIST}), 128 BPM, deck-row window",
            }
        }
    )
    findings = fixture_real_content_findings(rel, text)
    assert any(f.rule == "fixture-anlz-commercial" for f in findings)


def test_planted_test_pairs_owner_home_with_commercial_title() -> None:
    """[if] a test adds an owner home path and commercial title [then] fail, [else stop]."""
    rel = "tests/scripts/planted_owner_track.py"
    text = f'TRACK = "{_REAL_HOME}"\nTITLE = "{_STROBE}"\n'
    findings = fixture_real_content_findings(rel, text)
    assert any(f.rule == "fixture-test-owner-track" for f in findings)


def test_personal_jesus_shaped_lyrics_fixture_is_refused() -> None:
    """[if] a lyrics fixture matches personal-jesus-shaped content [then] fail, [else stop]."""
    rel = "tests/fixtures/lyrics_asr/planted_cache.json"
    text = json.dumps(
        {
            "stable_id": synthetic_stable_id("planted_cache.json"),
            "lines": [{"text": "your own personal savior line"}],
        }
    )
    findings = fixture_real_content_findings(rel, text)
    assert any(f.rule == "fixture-lyrics-commercial-shape" for f in findings)


def test_strobe_named_manifest_is_refused() -> None:
    """[if] a Strobe-named ANLZ manifest is reintroduced [then] fail, [else stop]."""
    rel = "apps/webui/frontend/tests/unit/fixtures/planted.manifest.json"
    text = json.dumps({"capture": {"title": _STROBE, "artist": _ARTIST}})
    findings = fixture_real_content_findings(rel, text)
    assert any(f.rule == "fixture-anlz-commercial" for f in findings)
