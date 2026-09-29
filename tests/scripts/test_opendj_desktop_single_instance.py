"""INSTALL-24: diagnostic flags exit before single-instance admission.

The live binary opens a window, so these checks read the shipped `main.rs`
source the way `test_desktop_lane_config.py` reads the dmg recipe: the
admission order is the contract, and swapping two calls is the mutation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
MAIN_RS: Path = REPO_ROOT / "apps/desktop/src-tauri/src/main.rs"


def _source() -> str:
    return MAIN_RS.read_text(encoding="utf-8")


def _main_fn_body() -> str:
    text = _source()
    start = text.index("fn main() {")
    end = text.index("#[cfg(test)]")
    return text[start:end]


def _parse_fn_body() -> str:
    text = _source()
    start = text.index("fn parse_diagnostic_flag")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


# REQ: INSTALL-24
@pytest.mark.requirement("INSTALL-24")
def test_help_and_version_map_to_diagnostic_flags() -> None:
    """[if] -h/--help/--version appear [then] they map to diagnostic flags, [else stop]."""
    body = _parse_fn_body()
    assert '"-h" | "--help" => return Some(DiagnosticFlag::Help)' in body
    assert '"--version" => return Some(DiagnosticFlag::Version)' in body
    assert "_ => {}" in body
    assert body.rstrip().endswith("None\n}")


# REQ: INSTALL-24
@pytest.mark.requirement("INSTALL-24")
def test_diagnostic_flags_exit_before_signals_single_instance_and_data_dir() -> None:
    """[if] main starts [then] help/version exit before launch admission, [else stop]."""
    body = _main_fn_body()
    help_at = body.index("handle_diagnostic_flags")
    signals_at = body.index("install_signal_handlers()")
    builder_at = body.index("tauri::Builder::default()")
    instance_at = body.index("tauri_plugin_single_instance::init")
    data_dir_at = body.index("app_data_dir(")
    assert help_at < signals_at < builder_at < instance_at < data_dir_at
    after_builder = body[builder_at:]
    first_plugin = after_builder.index(".plugin(")
    plugin_call = after_builder[first_plugin : after_builder.index(")", first_plugin)]
    assert "tauri_plugin_single_instance::init" in plugin_call


# REQ: INSTALL-24
@pytest.mark.requirement("INSTALL-24")
def test_an_unrecognized_flag_is_a_normal_launch() -> None:
    """[if] argv is an unknown flag [then] parse falls through to launch, [else stop]."""
    body = _parse_fn_body()
    help_arm = body.index('"-h" | "--help"')
    version_arm = body.index('"--version"')
    fallthrough = body.index("_ => {}")
    none_at = body.rindex("None")
    assert help_arm < version_arm < fallthrough < none_at
    # A swallowed unknown flag would be a match arm that returns Some(_).
    assert "Some(DiagnosticFlag" not in body[fallthrough:none_at]
