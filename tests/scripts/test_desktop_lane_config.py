"""Lane-label overlay for the Open DJ desktop bundle.

Single-line acceptance checks, in the repo's "if X then broken" shape:

- if an unset label changes the bundle identifier, the unlabelled build is
  no longer the product build -> broken.
- if MDT_LANE_LABEL=B does not end the identifier in .lane-b, two lanes
  share Application Support on one Mac -> broken.
- if a label with a space, a slash or a dot is accepted, the build can ship
  under an unintended name or escape its directory -> broken.
- if the dmg filename carries a space or a parenthesis, every downstream
  shell command needs quoting -> broken.
- if the architecture is guessed rather than read from what Tauri built,
  an arm64/aarch64 mismatch ships in the filename -> broken.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.desktop_lane_config import (
    LaneLabelError,
    arch_from_built_name,
    dmg_filename,
    lane_identifier,
    lane_product_name,
    overlay,
    product_slug,
    validate_label,
)

BASE_PRODUCT: str = "Open DJ"
BASE_IDENTIFIER: str = "com.opendj.desktop"

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
TAURI_CONF: Path = REPO_ROOT / "apps/desktop/src-tauri/tauri.conf.json"


# ----- label validation --------------------------------------------------
@pytest.mark.parametrize("raw", [None, "", "   "])
def test_absent_label_is_none(raw: str | None) -> None:
    assert validate_label(raw) is None


@pytest.mark.parametrize("raw", ["B", "b", "A", "agentB", "2"])
def test_simple_labels_survive(raw: str) -> None:
    assert validate_label(raw) == raw.strip()


@pytest.mark.parametrize(
    "raw", ["b b", "../x", "-b", "b.", "b/c", "lane b", "b_c", "b-c", "é"]
)
def test_unsafe_labels_are_refused_not_sanitised(raw: str) -> None:
    with pytest.raises(LaneLabelError):
        validate_label(raw)


def test_label_is_trimmed_before_use() -> None:
    assert validate_label("  B  ") == "B"


# ----- identifier and product name ---------------------------------------
def test_unset_label_leaves_identifier_untouched() -> None:
    assert lane_identifier(BASE_IDENTIFIER, None) == BASE_IDENTIFIER


def test_label_suffixes_identifier_lowercased() -> None:
    assert lane_identifier(BASE_IDENTIFIER, "B") == "com.opendj.desktop.lane-b"


def test_identifier_suffix_is_case_insensitive_for_apple() -> None:
    # Apple treats identifiers case-insensitively, so 'B' and 'b' must not
    # produce two identifiers that collide on disk but differ in config.
    assert lane_identifier(BASE_IDENTIFIER, "B") == lane_identifier(
        BASE_IDENTIFIER, "b"
    )


def test_unset_label_leaves_product_name_untouched() -> None:
    assert lane_product_name(BASE_PRODUCT, None) == BASE_PRODUCT


def test_label_appears_in_product_name() -> None:
    assert lane_product_name(BASE_PRODUCT, "B") == "Open DJ (B)"


# ----- overlay -----------------------------------------------------------
def test_unlabelled_overlay_is_empty_so_the_build_is_unchanged() -> None:
    assert overlay(BASE_PRODUCT, BASE_IDENTIFIER, None) == {}


def test_labelled_overlay_sets_both_clash_keys() -> None:
    assert overlay(BASE_PRODUCT, BASE_IDENTIFIER, "B") == {
        "productName": "Open DJ (B)",
        "identifier": "com.opendj.desktop.lane-b",
    }


# ----- artifact naming ---------------------------------------------------
def test_product_slug_strips_the_space() -> None:
    assert product_slug(BASE_PRODUCT) == "OpenDJ"


def test_unlabelled_dmg_name() -> None:
    assert dmg_filename(BASE_PRODUCT, None, "0.1.0", "aarch64") == (
        "OpenDJ-0.1.0-aarch64.dmg"
    )


def test_labelled_dmg_name() -> None:
    assert dmg_filename(BASE_PRODUCT, "B", "0.1.0", "aarch64") == (
        "OpenDJ-B-0.1.0-aarch64.dmg"
    )


@pytest.mark.parametrize("label", [None, "B"])
def test_dmg_name_is_shell_safe(label: str | None) -> None:
    name = dmg_filename(BASE_PRODUCT, label, "0.1.0", "aarch64")
    assert " " not in name
    assert "(" not in name and ")" not in name


def test_arch_is_read_from_the_built_artifact() -> None:
    assert arch_from_built_name("Open DJ (B)_0.1.0_aarch64.dmg") == "aarch64"


def test_arch_refuses_a_name_it_cannot_parse() -> None:
    with pytest.raises(LaneLabelError):
        arch_from_built_name("opendj.dmg")


# ----- the real config ---------------------------------------------------
def test_shipped_config_bundles_a_dmg() -> None:
    """The work item itself: bundling must be on, with a dmg target."""
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    assert conf["bundle"]["active"] is True
    assert "dmg" in conf["bundle"]["targets"]


def test_shipped_config_is_the_unlabelled_product() -> None:
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    assert conf["productName"] == BASE_PRODUCT
    assert conf["identifier"] == BASE_IDENTIFIER


def test_shipped_config_points_at_the_bundled_setup_page() -> None:
    """The window must load the bundled page, never a bare engine URL.

    A window pointed straight at the engine shows a WebKit connection error
    when the engine is down, which is the blank-window failure the setup
    screen exists to replace.
    """
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    assert conf["build"]["frontendDist"] == "../setup"
    setup_dir = TAURI_CONF.parent / conf["build"]["frontendDist"]
    assert (setup_dir / "index.html").is_file()
    assert (setup_dir / "setup.js").is_file()


def test_setup_page_and_rust_agree_on_the_default_engine_origin() -> None:
    """One default, stated in two languages; drift would be silent."""
    setup_js = (TAURI_CONF.parent.parent / "setup/setup.js").read_text(
        encoding="utf-8"
    )
    main_rs = (TAURI_CONF.parent / "src/main.rs").read_text(encoding="utf-8")
    assert "export const DEFAULT_ENGINE_ORIGIN = 'http://127.0.0.1:8685';" in setup_js
    assert 'None => "http://127.0.0.1:8685",' in main_rs


# ----- CLI ---------------------------------------------------------------
def _run_cli(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.desktop_lane_config", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_cli_emits_an_empty_overlay_without_a_label() -> None:
    assert _run_cli("overlay", "--config", str(TAURI_CONF), "--label", "") == "{}"


def test_cli_emits_the_lane_overlay() -> None:
    emitted = json.loads(
        _run_cli("overlay", "--config", str(TAURI_CONF), "--label", "B")
    )
    assert emitted["identifier"] == "com.opendj.desktop.lane-b"
    assert emitted["productName"] == "Open DJ (B)"


def test_cli_names_the_labelled_artifact() -> None:
    name = _run_cli(
        "dmg-name",
        "--config",
        str(TAURI_CONF),
        "--label",
        "B",
        "--built",
        "Open DJ (B)_0.1.0_aarch64.dmg",
    )
    assert name == "OpenDJ-B-0.1.0-aarch64.dmg"


def test_cli_rejects_a_bad_label_loudly() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.desktop_lane_config",
            "overlay",
            "--config",
            str(TAURI_CONF),
            "--label",
            "lane b",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "MDT_LANE_LABEL" in result.stderr
