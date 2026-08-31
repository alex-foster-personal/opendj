"""Named build profiles: the App Store build is `--build-profile appstore`.

  [if] an unknown profile name defaults to the full build instead of raising,
       a packaging typo ships USB export enabled into a sandboxed bundle
       -> test_an_unknown_profile_is_refused_not_defaulted
  [if] a file in the data dir can override a packaged profile, a store build
       is re-enablable from outside -> test_a_profile_beats_a_data_dir_file
  [if] an explicit flags path stops winning, a lane or test loses its escape
       hatch -> test_an_explicit_path_beats_a_profile
  [if] the appstore profile stops turning usb.export off, the store build
       offers a feature the sandbox forbids
       -> test_the_appstore_profile_disables_usb_export
  [if] --build-profile stops reaching the flag store, the CLI flag is
       decorative -> test_the_cli_flag_selects_the_profile
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apps.engine_core.__main__ import _apply_build_profile, build_parser
from apps.engine_core.config import EngineBootError
from apps.feature_flags import profiles
from apps.feature_flags.store import FLAGS, flags_path, load_flags


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Isolate the two env vars BEFORE and AFTER every test in this module.

    After matters as much as before: `_apply_build_profile` writes straight to
    os.environ (that is its job -- one var has to reach create_app and every
    subprocess), so a test that calls it leaks the profile into every later
    test in the session. That leak turned seven unrelated flag tests red on
    the first full-suite run.
    """
    for var in (profiles.BUILD_PROFILE_ENV, "MDT_FEATURE_FLAGS_FILE"):
        monkeypatch.delenv(var, raising=False)
    yield
    for var in (profiles.BUILD_PROFILE_ENV, "MDT_FEATURE_FLAGS_FILE"):
        os.environ.pop(var, None)


def test_the_full_build_is_the_absence_of_a_profile() -> None:
    assert profiles.selected_profile({}) == profiles.DEFAULT_PROFILE
    assert profiles.profile_path(profiles.DEFAULT_PROFILE) is None


def test_appstore_is_a_shipped_profile() -> None:
    assert "appstore" in profiles.available_profiles()
    assert profiles.profile_path("appstore").is_file()


def test_an_unknown_profile_is_refused_not_defaulted() -> None:
    with pytest.raises(profiles.UnknownProfileError, match="unknown build profile"):
        profiles.profile_path("apstore")


def test_the_appstore_profile_disables_usb_export() -> None:
    payload = json.loads(profiles.profile_path("appstore").read_text())
    assert payload["usb.export"] is False
    declared = {flag.flag_id for flag in FLAGS}
    assert set(payload) <= declared, "profile sets a flag nobody declared"


def test_a_profile_beats_a_data_dir_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The security-shaped half of the precedence order.

    A store build must not be re-enabled by dropping a file into the data dir,
    so the packaged profile outranks it.
    """
    (tmp_path / "feature-flags.json").write_text('{"usb.export": true}')
    monkeypatch.setenv(profiles.BUILD_PROFILE_ENV, "appstore")
    assert flags_path(tmp_path) == profiles.profile_path("appstore")
    assert load_flags(tmp_path).enabled("usb.export") is False


def test_an_explicit_path_beats_a_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane or a test can always point somewhere scratch."""
    scratch = tmp_path / "lane.json"
    scratch.write_text('{"usb.export": true}')
    monkeypatch.setenv(profiles.BUILD_PROFILE_ENV, "appstore")
    monkeypatch.setenv("MDT_FEATURE_FLAGS_FILE", str(scratch))
    assert flags_path(tmp_path) == scratch
    assert load_flags(tmp_path).enabled("usb.export") is True


def test_no_profile_reads_the_data_dir_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "feature-flags.json").write_text('{"usb.export": false}')
    assert flags_path(tmp_path) == tmp_path / "feature-flags.json"
    assert load_flags(tmp_path).enabled("usb.export") is False


# ----- the CLI flag -------------------------------------------------------


def test_the_cli_flag_selects_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    args = build_parser().parse_args(
        ["serve", "--data-dir", "/tmp/x", "--port", "1", "--build-profile", "appstore"]
    )
    assert args.build_profile == "appstore"
    _apply_build_profile(args.build_profile)
    assert profiles.selected_profile() == "appstore"


def test_the_cli_flag_refuses_an_unknown_profile_at_boot() -> None:
    """EngineBootError, so it exits 2 with the runbook, not a stack trace."""
    with pytest.raises(EngineBootError, match="unknown build profile"):
        _apply_build_profile("apstore")


def test_omitting_the_flag_changes_nothing() -> None:
    _apply_build_profile(None)
    assert profiles.selected_profile() == profiles.DEFAULT_PROFILE
