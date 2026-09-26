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
- if the architecture is defaulted rather than passed in, an arm64/aarch64
  mismatch ships in the filename -> broken.
- if the recipe creates its image only inside the signing branch, an unsigned
  build finishes having produced no artifact at all -> broken.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.desktop_lane_config import (
    LaneLabelError,
    dmg_filename,
    lane_identifier,
    lane_product_name,
    manifest_stamp,
    overlay,
    product_slug,
    validate_label,
)

BASE_PRODUCT: str = "Open DJ"
BASE_IDENTIFIER: str = "com.opendj.desktop"

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
TAURI_CONF: Path = REPO_ROOT / "apps/desktop/src-tauri/tauri.conf.json"
JUSTFILE: Path = REPO_ROOT / "justfile"


# ----- label validation --------------------------------------------------
@pytest.mark.requirement("INSTALL-06")
@pytest.mark.parametrize("raw", [None, "", "   "])
def test_absent_label_is_none(raw: str | None) -> None:
    assert validate_label(raw) is None


@pytest.mark.requirement("INSTALL-06")
@pytest.mark.parametrize("raw", ["B", "b", "A", "agentB", "2"])
def test_simple_labels_survive(raw: str) -> None:
    assert validate_label(raw) == raw.strip()


@pytest.mark.parametrize("raw", ["b b", "../x", "-b", "b.", "b/c", "lane b", "b_c", "b-c", "é"])
@pytest.mark.requirement("INSTALL-06")
def test_unsafe_labels_are_refused_not_sanitised(raw: str) -> None:
    with pytest.raises(LaneLabelError):
        validate_label(raw)


@pytest.mark.requirement("INSTALL-06")
def test_label_is_trimmed_before_use() -> None:
    assert validate_label("  B  ") == "B"


# ----- identifier and product name ---------------------------------------
@pytest.mark.requirement("INSTALL-06")
def test_unset_label_leaves_identifier_untouched() -> None:
    assert lane_identifier(BASE_IDENTIFIER, None) == BASE_IDENTIFIER


@pytest.mark.requirement("INSTALL-06")
def test_label_suffixes_identifier_lowercased() -> None:
    assert lane_identifier(BASE_IDENTIFIER, "B") == "com.opendj.desktop.lane-b"


@pytest.mark.requirement("INSTALL-06")
def test_identifier_suffix_is_case_insensitive_for_apple() -> None:
    # Apple treats identifiers case-insensitively, so 'B' and 'b' must not
    # produce two identifiers that collide on disk but differ in config.
    assert lane_identifier(BASE_IDENTIFIER, "B") == lane_identifier(BASE_IDENTIFIER, "b")


@pytest.mark.requirement("INSTALL-06")
def test_unset_label_leaves_product_name_untouched() -> None:
    assert lane_product_name(BASE_PRODUCT, None) == BASE_PRODUCT


@pytest.mark.requirement("INSTALL-06")
def test_label_appears_in_product_name() -> None:
    assert lane_product_name(BASE_PRODUCT, "B") == "Open DJ (B)"


# ----- overlay -----------------------------------------------------------
@pytest.mark.requirement("INSTALL-06")
def test_unlabelled_overlay_is_empty_so_the_build_is_unchanged() -> None:
    assert overlay(BASE_PRODUCT, BASE_IDENTIFIER, None) == {}


@pytest.mark.requirement("INSTALL-06")
def test_labelled_overlay_sets_both_clash_keys() -> None:
    assert overlay(BASE_PRODUCT, BASE_IDENTIFIER, "B") == {
        "productName": "Open DJ (B)",
        "identifier": "com.opendj.desktop.lane-b",
    }


# ----- artifact naming ---------------------------------------------------
def test_product_slug_strips_the_space() -> None:
    assert product_slug(BASE_PRODUCT) == "OpenDJ"


def test_unlabelled_dmg_name() -> None:
    assert dmg_filename(BASE_PRODUCT, None, "0.1.0", "aarch64") == ("OpenDJ-0.1.0-aarch64.dmg")


def test_labelled_dmg_name() -> None:
    assert dmg_filename(BASE_PRODUCT, "B", "0.1.0", "aarch64") == ("OpenDJ-B-0.1.0-aarch64.dmg")


@pytest.mark.parametrize("label", [None, "B"])
def test_dmg_name_is_shell_safe(label: str | None) -> None:
    name = dmg_filename(BASE_PRODUCT, label, "0.1.0", "aarch64")
    assert " " not in name
    assert "(" not in name and ")" not in name


@pytest.mark.requirement("INSTALL-05")
def test_arch_is_refused_rather_than_defaulted() -> None:
    """An unstated architecture is refused, never assumed.

    The architecture used to be read off the filename of the dmg Tauri
    bundled. That artifact is gone (#1711), so the caller states the value
    instead -- and a caller that forgets gets an error rather than a
    plausible-looking name for a machine that cannot run it.
    """
    with pytest.raises(LaneLabelError):
        dmg_filename(BASE_PRODUCT, "B", "0.1.0", "")


# ----- the real config ---------------------------------------------------
# REQ: INSTALL-16
def test_shipped_config_does_not_ask_tauri_for_a_dmg() -> None:
    """#1711: Tauri's dmg target cannot be built where nobody is logged in.

    ``bundle_dmg.sh`` drives Finder over AppleScript to lay the image's
    window out. Finder does not answer Apple events from a non-interactive
    context on silver, so the four measured runs of Thu 10 Sep 2026 all
    ended in that script with

        execution error: Finder got an error: AppleEvent timed out. (-1712)

    and it fails identically inside the console Aqua session, with Finder
    running and a console user logged in. The target is therefore gone and
    the image is produced by ``hdiutil create`` in the ``dmg`` recipe
    instead, which is a plain file operation and needs no logged-in user.

    Bundling itself must stay ON: with ``active`` false there is no .app at
    all and the recipe has nothing to package.
    """
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    assert conf["bundle"]["active"] is True
    assert conf["bundle"]["targets"] == ["app"]


# ----- the real recipe ---------------------------------------------------
DMG_RECIPE_HEADER: str = "dmg lane='':"


def _dmg_recipe_body() -> list[str]:
    """The ``dmg`` recipe's shell lines, comments and blanks stripped out."""
    lines = JUSTFILE.read_text(encoding="utf-8").splitlines()
    body: list[str] = []
    for line in lines[lines.index(DMG_RECIPE_HEADER) + 1 :]:
        if line and not line.startswith(("    ", "\t")):
            break  # the next top-level item: a recipe or a variable
        if line.strip() and not line.strip().startswith("#"):
            body.append(line)
    return body


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


# REQ: INSTALL-16
def test_the_image_is_created_outside_the_signing_branch() -> None:
    """An unsigned run must still produce the artifact.

    Dropping the Tauri ``dmg`` target removes the file the recipe used to
    pick up with ``ls -t .../bundle/dmg/*.dmg``. On the SIGNED path the
    recipe already replaced it with ``hdiutil create``; on the UNSIGNED
    path (``MDT_SHIP_UNSIGNED=1``, so ``identity`` is empty) that bundled
    image WAS the deliverable and was never recreated. Leaving the
    ``hdiutil create`` inside ``if [ -n "$identity" ]`` would therefore
    make an unsigned run finish having built no artifact at all.

    Nesting is what this asserts: the create must sit at the same indent
    as the signing branch, so it belongs to neither arm of it.
    """
    body = _dmg_recipe_body()
    creates = [line for line in body if line.strip().startswith("hdiutil create")]
    assert len(creates) == 1, creates
    guards = [line for line in body if line.strip() == 'if [ -n "$identity" ]; then']
    assert guards, "the signing branch vanished from the dmg recipe"
    assert _indent(creates[0]) == min(_indent(line) for line in guards)


def test_the_recipe_clears_the_app_bundle_before_building() -> None:
    """A stale app from another lane must not be the one packaged.

    ``source_app`` is chosen with ``find ... -print -quit``, the first match
    win. A lane overlay renames the .app, so a bundle left behind by a
    differently labelled build sits beside this run's and can win that race.
    Tauri's own output is only ever identified by the path it was just
    written to, so the fix is to clear the directory first -- which has to
    happen BEFORE the bundler runs, or it deletes this run's own app.
    """
    body = _dmg_recipe_body()
    clears = [
        index
        for index, line in enumerate(body)
        if "bundle/macos" in line and line.strip().startswith("rm ")
    ]
    builds = [index for index, line in enumerate(body) if "cargo tauri build" in line]
    assert len(clears) == 1, [body[index] for index in clears]
    assert builds, "the bundler invocation vanished from the dmg recipe"
    assert clears[0] < builds[0]


def test_the_recipe_derives_nothing_from_a_directory_listing() -> None:
    """The discarded artifact's filename was the only reason to list.

    ``arch_from_built_name`` parsed ``<productName>_<version>_<arch>.dmg``
    out of the newest file in ``bundle/dmg/``. With no bundled dmg there is
    nothing to list, so a surviving ``ls -t`` there would be reading a
    directory nothing writes to.
    """
    assert "ls -t" not in "".join(_dmg_recipe_body())


def test_shipped_config_is_the_unlabelled_product() -> None:
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    assert conf["productName"] == BASE_PRODUCT
    assert conf["identifier"] == BASE_IDENTIFIER


@pytest.mark.requirement("INSTALL-02")
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


@pytest.mark.requirement("INSTALL-14")
def test_the_shell_bakes_no_default_engine_origin() -> None:
    """The shell used to carry a hardcoded fallback port. It must not now.

    A build that starts its own engine on an OS-assigned port has no default
    address to bake, and a baked one is actively dangerous: two lanes on one
    Mac would both fall back to the same number and each other's engine would
    answer first. The setup page keeps its own default because it is also
    openable in a plain browser, where nothing injects an origin.
    """
    setup_js = (TAURI_CONF.parent.parent / "setup/setup.js").read_text(encoding="utf-8")
    main_rs = (TAURI_CONF.parent / "src/main.rs").read_text(encoding="utf-8")
    assert "export const DEFAULT_ENGINE_ORIGIN = 'http://127.0.0.1:8685';" in setup_js
    assert "DEFAULT_ENGINE_ORIGIN" not in main_rs
    assert "127.0.0.1:8685" not in main_rs
    # The operator seam survives: an explicitly supplied origin still wins,
    # which is how a packaged build gets driven against a chosen engine.
    assert 'ENGINE_ORIGIN_ENV: &str = "OPENDJ_ENGINE_ORIGIN"' in main_rs


@pytest.mark.requirement("PERFMODE-11")
def test_bootstrap_lands_on_performance() -> None:
    """Cold launch lands in Gig (issue #2698); supersedes AGENT-12 library-root pin."""
    setup_js = (TAURI_CONF.parent.parent / "setup/setup.js").read_text(encoding="utf-8")
    assert "navigate(`${origin}/performance`)" in setup_js
    assert "navigate(`${origin}/`)" not in setup_js


# ----- the bundled engine ------------------------------------------------
@pytest.mark.requirement("INSTALL-14")
def test_the_bundle_stages_the_engine_payload_into_resources() -> None:
    """Without this the dmg is a window onto somebody else's dev server."""
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    assert conf["bundle"]["resources"] == {"payload": "payload"}


@pytest.mark.requirement("INSTALL-14")
def test_the_payload_staging_dir_is_never_committed() -> None:
    """174MB of derived interpreter must not be able to enter git."""
    ignore = (TAURI_CONF.parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "src-tauri/payload/" in ignore


@pytest.mark.requirement("INSTALL-14")
def test_the_shell_picks_its_port_from_the_os() -> None:
    """A hardcoded port collides with the other lane and every dev server."""
    engine_rs = (TAURI_CONF.parent / "src/engine.rs").read_text(encoding="utf-8")
    assert 'TcpListener::bind("127.0.0.1:0")' in engine_rs
    # Port 0 means "OS, pick one". Any real port number written next to the
    # loopback address would be the hardcoding this replaces.
    assert re.search(r"127\.0\.0\.1:\d{2,}", engine_rs) is None


@pytest.mark.requirement("INSTALL-13")
def test_the_shell_strips_writeback_before_spawning_the_engine() -> None:
    """One-way safety is absolute: the shell removes an inherited opt-in.

    A build launched from a developer's terminal inherits that terminal's
    exported .env. Declining to SET the flag is not enough when it may
    already be set.
    """
    engine_rs = (TAURI_CONF.parent / "src/engine.rs").read_text(encoding="utf-8")
    assert '"MDT_REKORDBOX_WRITEBACK_ENABLED"' in engine_rs
    assert '"MDT_DATA_DIR"' in engine_rs
    assert "env_remove" in engine_rs


@pytest.mark.requirement("ERRCAP-01")
def test_the_shell_routes_its_own_diagnostics_into_engine_log() -> None:
    """Bundled launches from Finder have no terminal; shell output must land in engine.log."""
    main_rs = (TAURI_CONF.parent / "src/main.rs").read_text(encoding="utf-8")
    engine_rs = (TAURI_CONF.parent / "src/engine.rs").read_text(encoding="utf-8")
    assert "install_shell_logging" in main_rs
    assert "append_shell_log" in main_rs
    assert "panic::set_hook" in engine_rs
    assert ".on_page_load(" in main_rs
    assert "__OPENDJ_enqueueShellClientError" in main_rs


@pytest.mark.requirement("INSTALL-14")
def test_a_failed_boot_raises_a_dialog_rather_than_a_blank_window() -> None:
    """The blank window IS the bug; the shell must have no path to it."""
    main_rs = (TAURI_CONF.parent / "src/main.rs").read_text(encoding="utf-8")
    engine_rs = (TAURI_CONF.parent / "src/engine.rs").read_text(encoding="utf-8")
    assert "rfd::MessageDialog::new()" in main_rs
    assert "std::process::exit(1)" in main_rs
    # The window is only ever built after a healthy engine, so the failure
    # path cannot reach WebviewWindowBuilder.
    assert main_rs.index("fail_visibly(&failure)") < main_rs.index("WebviewWindowBuilder::new")
    assert "wait_until_healthy" in engine_rs


@pytest.mark.requirement("INSTALL-14")
def test_the_engine_is_killed_as_a_process_group_on_exit() -> None:
    """Otherwise a job outlives the app and holds the data dir's lock."""
    main_rs = (TAURI_CONF.parent / "src/main.rs").read_text(encoding="utf-8")
    engine_rs = (TAURI_CONF.parent / "src/engine.rs").read_text(encoding="utf-8")
    assert ".process_group(0)" in engine_rs
    assert "libc::killpg(pid, libc::SIGTERM)" in engine_rs
    assert "libc::killpg(pid, libc::SIGKILL)" in engine_rs
    assert "RunEvent::Exit" in main_rs
    assert "supervisor.shutdown()" in main_rs


# ----- build identity ----------------------------------------------------
@pytest.mark.requirement("INSTALL-07")
def test_the_shell_stamps_itself_at_compile_time() -> None:
    """Both halves must be able to say what they are, separately."""
    main_rs = (TAURI_CONF.parent / "src/main.rs").read_text(encoding="utf-8")
    build_rs = (TAURI_CONF.parent / "build.rs").read_text(encoding="utf-8")
    stamped = [
        "OPENDJ_BUILD_GIT_SHA",
        "OPENDJ_BUILD_GIT_SHA_FULL",
        "OPENDJ_BUILD_GIT_BRANCH",
        "OPENDJ_BUILD_GIT_DIRTY",
        "OPENDJ_BUILD_AT_UTC",
        "OPENDJ_BUILD_LANE_LABEL",
        "OPENDJ_BUILD_CHANNEL",
        "OPENDJ_BUILD_EVIDENCE_AT_UTC",
    ]
    for name in stamped:
        assert f'option_env!("{name}")' in main_rs, name
        # cargo does not track option_env! reads on its own, so a stamp
        # missing from build.rs would leave the PREVIOUS value compiled in --
        # the exact staleness this readout exists to expose.
        assert f'"{name}",' in build_rs, name
    assert "cargo:rerun-if-env-changed=" in build_rs
    assert "globalThis.OPENDJ_SHELL_BUILD" in main_rs
    # An unstamped shell must say so rather than invent a sha.
    assert '"stamped": BUILD_GIT_SHA.is_some()' in main_rs


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


@pytest.mark.requirement("INSTALL-06")
def test_cli_emits_an_empty_overlay_without_a_label() -> None:
    assert _run_cli("overlay", "--config", str(TAURI_CONF), "--label", "") == "{}"


@pytest.mark.requirement("INSTALL-06")
def test_cli_emits_the_lane_overlay() -> None:
    emitted = json.loads(_run_cli("overlay", "--config", str(TAURI_CONF), "--label", "B"))
    assert emitted["identifier"] == "com.opendj.desktop.lane-b"
    assert emitted["productName"] == "Open DJ (B)"


def test_cli_names_the_labelled_artifact() -> None:
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    expected = dmg_filename(BASE_PRODUCT, "B", conf["version"], "aarch64")
    name = _run_cli(
        "dmg-name",
        "--config",
        str(TAURI_CONF),
        "--label",
        "B",
        "--arch",
        "aarch64",
    )
    assert name == expected


def test_cli_refuses_dmg_name_without_an_arch() -> None:
    """No --arch means no name, not a guessed one."""
    with pytest.raises(subprocess.CalledProcessError) as refused:
        _run_cli("dmg-name", "--config", str(TAURI_CONF), "--label", "B")
    assert "needs --arch" in refused.value.stderr


@pytest.mark.requirement("INSTALL-04")
def test_notary_profile_without_a_signing_identity_is_refused() -> None:
    """Notarizing an unsigned app is impossible, so refuse before building.

    The guard runs ahead of the cargo build, so this costs about a second.
    """
    just_bin = shutil.which("just")
    if just_bin is None:
        pytest.skip("just is not installed")
    assert just_bin is not None
    # Scrub any ambient MDT_LANE_LABEL: this test targets the notary/signing
    # guard, not OPS-09's lane double-intent gate, and must not depend on
    # the invoking shell being free of a stray lane label.
    env = {k: v for k, v in os.environ.items() if k != "MDT_LANE_LABEL"}
    env["MDT_MACOS_NOTARY_KEYCHAIN_PROFILE"] = "some-profile"
    env["MDT_MACOS_SIGNING_IDENTITY"] = ""
    result = subprocess.run(
        [just_bin, "dmg"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "MDT_MACOS_NOTARY_KEYCHAIN_PROFILE is set" in combined
    # It must have refused BEFORE spending a build.
    assert "Compiling" not in combined


@pytest.mark.requirement("INSTALL-06")
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


# ----- manifest stamp emitter --------------------------------------------
@pytest.mark.requirement("INSTALL-07")
def test_stamp_reads_one_identity_field(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"identity": {"git_sha": "abc1234", "git_dirty": False}}),
        encoding="utf-8",
    )
    assert manifest_stamp(manifest, "git_sha") == "abc1234"


@pytest.mark.requirement("INSTALL-07")
def test_stamp_emits_booleans_rust_can_read(tmp_path: Path) -> None:
    """option_env! yields a string, and "False" is a true-ish string."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"identity": {"git_dirty": True}}), encoding="utf-8")
    assert manifest_stamp(manifest, "git_dirty") == "1"
    manifest.write_text(json.dumps({"identity": {"git_dirty": False}}), encoding="utf-8")
    assert manifest_stamp(manifest, "git_dirty") == "0"


@pytest.mark.requirement("INSTALL-07")
def test_stamp_refuses_a_null_rather_than_stamping_one(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"identity": {"lane_label": None}}), encoding="utf-8")
    with pytest.raises(LaneLabelError):
        manifest_stamp(manifest, "lane_label")


@pytest.mark.requirement("INSTALL-07")
def test_stamp_names_the_fields_it_does_have(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"identity": {"git_sha": "abc"}}), encoding="utf-8")
    with pytest.raises(LaneLabelError, match="git_sha"):
        manifest_stamp(manifest, "git_shaa")


@pytest.mark.requirement("INSTALL-07")
def test_the_dmg_recipe_stamps_the_shell_from_the_payload_manifest() -> None:
    """One git read for both halves; two reads can disagree."""
    recipe = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    for name in (
        "OPENDJ_BUILD_GIT_SHA",
        "OPENDJ_BUILD_GIT_SHA_FULL",
        "OPENDJ_BUILD_GIT_BRANCH",
        "OPENDJ_BUILD_GIT_DIRTY",
        "OPENDJ_BUILD_AT_UTC",
        "OPENDJ_BUILD_LANE_LABEL",
        "OPENDJ_BUILD_CHANNEL",
        "OPENDJ_BUILD_EVIDENCE_AT_UTC",
    ):
        assert f"export {name}=$(stamp " in recipe, name
    # The dmg must not be assembleable around a payload from another commit.
    assert 'rm -rf "$payload"' in recipe
    assert 'scripts.build_engine_payload --out "$payload"' in recipe
    assert '[ "$shipped_sha" = "$OPENDJ_BUILD_GIT_SHA" ]' in recipe


@pytest.mark.requirement("OPS-08")
def test_the_dmg_recipe_accepts_an_unset_lane_label() -> None:
    """OPS-08: unset builds the plain Open DJ; the old refusal is gone.

    The label guard used to protect two bake-off lanes from sharing one
    library. With the bake-off over, an unset label IS the product build,
    so the recipe must neither refuse it nor default it to a lane.
    """
    recipe = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    assert "MDT_LANE_LABEL is unset" not in recipe
    assert 'label="${MDT_LANE_LABEL:-}"' in recipe  # override survives
