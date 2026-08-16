"""Regression tests for codex CONFIRMED-FOLLOWUP findings, group C.

Covers:
* P09-F02 -- Spotify writers pin ``encoding="utf-8"`` so non-ASCII
  track/artist names survive locales where the default encoding is
  not UTF-8.
* P10-F03 -- ``apps.sync.usb.preflight.preflight`` runs the
  ``no_other_writer`` probe in non-write_probe mode (warning) and
  promotes it to an error in write_probe (apply) mode.
* P11-F03 -- pure UI glitch in the Svelte store; structural smoke
  test on the source asserts the per-toast id is captured in the
  closure rather than the module-level sequence counter.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync.usb import preflight as pf_mod
from apps.sync.usb.diff import Plan
from apps.sync.usb.preflight import preflight
from apps.sync.usb.profile import load_from_string


# Requirement IDs come from reqs.json. There is no SPOTIFY/USB category --
# the Spotify importer is CAT-01, USB sync + verify is CAT-02, and the web UI
# the toast store belongs to is CAT-05.
pytestmark = [
    pytest.mark.requirement("CAT-01"),  # P09-F02 Spotify writer encoding
    pytest.mark.requirement("CAT-02"),  # P10-F03 USB preflight writer probe
    pytest.mark.requirement("CAT-05"),  # P11-F03 web UI toast store
]


_PROFILE_YAML = """\
name: fixtureA
drive_label: FIXTURE-A
playlists:
  - Warmup
  - Peak
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: canonical-wins
exclusions: []
"""


@pytest.fixture
def fixture_profile():
    return load_from_string(_PROFILE_YAML)


@pytest.fixture
def drive_root(tmp_path: Path) -> Path:
    root = tmp_path / "FIXTURE-A"
    root.mkdir()
    return root


# -- P09-F02 --------------------------------------------------------------


def test_p09_f02_spotify_writers_pass_utf8(tmp_path: Path) -> None:
    """Assert the three patched writers reference ``encoding="utf-8"``."""
    repo = Path(__file__).resolve().parents[1]
    for rel in (
        "apps/spotify/acquisition.py",
        "apps/spotify/reporting.py",
        "apps/spotify/state_writer.py",
    ):
        text = (repo / rel).read_text(encoding="utf-8")
        assert 'encoding="utf-8"' in text, (
            f"{rel} must pin encoding='utf-8' for P09-F02"
        )


# -- P10-F03 --------------------------------------------------------------


def _small_plan(drive_root: Path) -> Plan:
    return Plan(
        profile_name="fixtureA",
        drive_root=drive_root,
        ops=[],
        total_bytes=0,
        existing_bytes=0,
        free_bytes_needed=0,
    )


def test_p10_f03_no_other_writer_errors_on_apply(
    monkeypatch: pytest.MonkeyPatch, fixture_profile, drive_root: Path
) -> None:
    monkeypatch.setattr(
        pf_mod, "_lsof_writers", lambda _root: ["other-process 1234"]
    )
    res = preflight(
        fixture_profile,
        _small_plan(drive_root),
        drive_root=drive_root,
        write_probe=True,
        drive_free_bytes=10_000_000_000,
    )
    assert any("no_other_writer" in e for e in res.errors), (
        f"write_probe=True must promote no_other_writer to error; got "
        f"errors={res.errors} warnings={res.warnings}"
    )


def test_p10_f03_no_other_writer_warns_on_readonly(
    monkeypatch: pytest.MonkeyPatch, fixture_profile, drive_root: Path
) -> None:
    monkeypatch.setattr(
        pf_mod, "_lsof_writers", lambda _root: ["other-process 1234"]
    )
    res = preflight(
        fixture_profile,
        _small_plan(drive_root),
        drive_root=drive_root,
        write_probe=False,
        drive_free_bytes=10_000_000_000,
    )
    assert any("no_other_writer" in w for w in res.warnings), (
        f"write_probe=False must emit no_other_writer warning; got "
        f"errors={res.errors} warnings={res.warnings}"
    )
    assert not any("no_other_writer" in e for e in res.errors)


# -- P11-F03 --------------------------------------------------------------


def test_p11_f03_toast_captures_own_id() -> None:
    """The toast store must dismiss the correct toast on overlap.

    We cannot evaluate Svelte runes inside Python, but we can assert the
    pushToast closure uses a captured ``id`` rather than ``_toastSeq``
    in its setTimeout callback.
    """
    repo = Path(__file__).resolve().parents[1]
    text = (
        repo / "apps/webui/frontend/src/lib/stores.svelte.ts"
    ).read_text(encoding="utf-8")
    # The bug was `t.id === _toastSeq` inside the setTimeout callback.
    assert "t.id === _toastSeq" not in text, (
        "setTimeout must not close over the module-level sequence counter"
    )
    # After the fix we capture the id in a local `id` const.
    assert "t.id === id" in text
