"""Fail-fast enum for MUSIC_DJ_LIBRARY_JOBS (issue #5559)."""

from __future__ import annotations

import pytest

from apps.webui.server import library_jobs_autostart as lj


def test_arm_from_environ_is_a_fail_fast_enum() -> None:
    """[if] MUSIC_DJ_LIBRARY_JOBS is unset, " ON ", "off", or "maybe" [then] arm / arm / disarm / ValueError naming the var, [else stop]."""
    assert lj.arm_from_environ({}) is True
    assert lj.arm_from_environ({lj.LIBRARY_JOBS_ENV: " ON "}) is True
    assert lj.arm_from_environ({lj.LIBRARY_JOBS_ENV: "off"}) is False
    with pytest.raises(ValueError, match=lj.LIBRARY_JOBS_ENV):
        lj.arm_from_environ({lj.LIBRARY_JOBS_ENV: "maybe"})
