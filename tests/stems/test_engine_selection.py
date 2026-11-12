"""Stem engine selection with a crash blacklist file (STEM-12244).

[if] an engine subprocess crashes [then] selection refuses it until expiry or clear, [else stop].
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from apps.stems.crash_blacklist import (
    EngineCrashed,
    clear_entry,
    load_blacklist,
    record_crash,
    run_engine,
)
from apps.stems.selection import NoEligibleEngine, select_engine

pytestmark = pytest.mark.requirement("STEM-12244")

NOW_S: float = 1_700_000_000.0
TTL_S: float = 3600.0
PREFERRED: tuple[str, ...] = ("demucs", "spleeter")


def test_blacklisted_preferred_engine_is_refused(tmp_path: Path) -> None:
    """[if] the preferred engine has a current entry [then] selection skips it, [else stop]."""
    path = tmp_path / "crash_blacklist.json"
    record_crash(path, "demucs", 1, "demucs", NOW_S, TTL_S)
    assert select_engine(PREFERRED, set(PREFERRED), path, NOW_S + 10) == "spleeter"
    with pytest.raises(NoEligibleEngine, match="demucs: crashed"):
        select_engine(("demucs",), {"demucs"}, path, NOW_S + 10)


def test_real_failing_subprocess_is_recorded(tmp_path: Path) -> None:
    """[if] a real subprocess exits non-zero [then] its engine id lands in the blacklist file, [else stop]."""
    path = tmp_path / "state" / "crash_blacklist.json"
    with pytest.raises(EngineCrashed) as caught:
        run_engine(path, "demucs", [sys.executable, "-c", "import sys; sys.exit(3)"], NOW_S, TTL_S)
    assert caught.value.exit_code == 3
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["entries"]["demucs"]["exit_code"] == 3
    assert load_blacklist(path)["demucs"].expires_at_s == NOW_S + TTL_S
    run_engine(path, "spleeter", [sys.executable, "-c", "pass"], NOW_S, TTL_S)
    assert "spleeter" not in load_blacklist(path)
    assert select_engine(PREFERRED, set(PREFERRED), path, NOW_S + 1) == "spleeter"


def test_expired_or_cleared_entry_no_longer_blocks(tmp_path: Path) -> None:
    """[if] a blacklist entry has expired or been cleared [then] its engine is selectable again, [else stop]."""
    path = tmp_path / "crash_blacklist.json"
    record_crash(path, "demucs", 1, "demucs", NOW_S, TTL_S)
    assert select_engine(PREFERRED, set(PREFERRED), path, NOW_S + TTL_S) == "demucs"
    assert clear_entry(path, "demucs") is True
    assert select_engine(PREFERRED, set(PREFERRED), path, NOW_S + 1) == "demucs"


def test_no_eligible_engine_fails_closed(tmp_path: Path) -> None:
    """[if] no eligible engine remains [then] selection raises, never a fallback, [else stop]."""
    path = tmp_path / "crash_blacklist.json"
    record_crash(path, "demucs", 2, "demucs", NOW_S, TTL_S)
    with pytest.raises(NoEligibleEngine, match="spleeter: not eligible"):
        select_engine(PREFERRED, {"demucs"}, path, NOW_S + 1)
    with pytest.raises(NoEligibleEngine):
        select_engine((), {"demucs"}, path, NOW_S + 1)
