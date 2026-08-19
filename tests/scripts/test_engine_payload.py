"""The engine payload builder: what it refuses to ship.

Single-line acceptance checks, in the repo's "if X then broken" shape:

- if an unset lane label builds anyway, two lanes share one Application
  Support directory on the tester's Mac -> broken.
- if a stale or missing SPA build packages silently, the artifact ships a
  bundle nobody built -> broken.
- if the payload can carry zero or two SignalsmithStretch assets, deck load
  is either dead or racing a transformed twin -> broken.
- if a Mach-O file linking /opt/homebrew or /usr/local passes the scan, the
  bundle only runs on a machine with dev libraries -> broken.
- if a ctypes find_library site can pass unclassified, a library resolved on
  the tester's machine ships unexamined -> broken.
- if excluding a dependency leaves its private sub-dependencies installed,
  the exclusion was cosmetic -> broken.
- if the manifest's identity keys drift from what /api/v1/build-info
  requires, a shipped payload serves 503 for its own identity -> broken.
- if the app copy descends into the payload's own output directory, the
  build recurses until the filesystem refuses the path -> broken.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apps.engine_core.build_info import REQUIRED_IDENTITY_KEYS
from scripts.build_engine_payload import (
    EXCLUDED_DEPENDENCIES,
    LockedRequirement,
    PayloadBuildError,
    RuntimeLoadSite,
    assert_spa_is_fresh,
    classify_runtime_load_sites,
    find_runtime_load_sites,
    git_identity,
    link_violations,
    parse_locked_export,
    parse_otool,
    prune_excluded,
    sha256_tree,
    skip_output_tree,
    sole_stretch_asset,
)
from scripts.desktop_lane_config import LaneLabelError, require_label

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


# ----- lane label is a build input, not a default ------------------------
@pytest.mark.requirement("INSTALL-06")
@pytest.mark.parametrize("raw", [None, "", "   "])
def test_absent_lane_label_stops_the_build(raw: str | None) -> None:
    with pytest.raises(LaneLabelError) as excinfo:
        require_label(raw)
    assert "MDT_LANE_LABEL" in str(excinfo.value)


@pytest.mark.requirement("INSTALL-06")
def test_a_present_label_is_returned_unchanged() -> None:
    assert require_label("  B  ") == "B"


@pytest.mark.requirement("INSTALL-06")
def test_an_unsafe_label_is_still_refused_not_defaulted() -> None:
    with pytest.raises(LaneLabelError):
        require_label("lane b")


# ----- SPA freshness -----------------------------------------------------
def _frontend(tmp_path: Path, *, built: bool = True) -> Path:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src/app.ts").write_text("//", encoding="utf-8")
    if built:
        assets = frontend / "build/_app/immutable/assets"
        assets.mkdir(parents=True)
        (frontend / "build/index.html").write_text("<!doctype html>", encoding="utf-8")
        (assets / "SignalsmithStretch.abc123.mjs").write_text("//", encoding="utf-8")
    return frontend


@pytest.mark.requirement("INSTALL-08")
def test_a_missing_spa_build_stops_the_build(tmp_path: Path) -> None:
    with pytest.raises(PayloadBuildError) as excinfo:
        assert_spa_is_fresh(_frontend(tmp_path, built=False))
    assert "pnpm" in str(excinfo.value)


@pytest.mark.requirement("INSTALL-08")
def test_an_empty_spa_build_stops_the_build(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path, built=False)
    (frontend / "build").mkdir()
    with pytest.raises(PayloadBuildError):
        assert_spa_is_fresh(frontend)


@pytest.mark.requirement("INSTALL-08")
def test_a_fresh_spa_build_passes(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    assert assert_spa_is_fresh(frontend) == frontend / "build"


@pytest.mark.requirement("INSTALL-08")
def test_source_newer_than_build_stops_the_build_and_is_never_rebuilt(
    tmp_path: Path,
) -> None:
    frontend = _frontend(tmp_path)
    edited = frontend / "src/app.ts"
    build_mtime = (frontend / "build/index.html").stat().st_mtime
    import os

    os.utime(edited, (build_mtime + 60, build_mtime + 60))
    with pytest.raises(PayloadBuildError) as excinfo:
        assert_spa_is_fresh(frontend)
    assert "app.ts" in str(excinfo.value)
    assert "Rebuild the frontend" in str(excinfo.value)


# ----- the deck-load asset ------------------------------------------------
@pytest.mark.requirement("INSTALL-09")
def test_exactly_one_stretch_asset_is_accepted(tmp_path: Path) -> None:
    build = _frontend(tmp_path) / "build"
    assert sole_stretch_asset(build).name == "SignalsmithStretch.abc123.mjs"


@pytest.mark.requirement("INSTALL-09")
def test_zero_stretch_assets_is_a_regression(tmp_path: Path) -> None:
    build = _frontend(tmp_path) / "build"
    next(iter((build / "_app/immutable/assets").glob("Signalsmith*"))).unlink()
    with pytest.raises(PayloadBuildError) as excinfo:
        sole_stretch_asset(build)
    assert "cannot load a deck" in str(excinfo.value)


@pytest.mark.requirement("INSTALL-09")
def test_two_stretch_assets_is_a_regression(tmp_path: Path) -> None:
    build = _frontend(tmp_path) / "build"
    (build / "_app/immutable/assets/SignalsmithStretch.def456.mjs").write_text(
        "//", encoding="utf-8"
    )
    with pytest.raises(PayloadBuildError):
        sole_stretch_asset(build)


# ----- linked libraries ---------------------------------------------------
DYLIB_OUTPUT: str = (
    "/p/lib/libfoo.dylib:\n"
    "\t/build/machine/only/libfoo.dylib (compatibility version 1.0.0)\n"
    "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
)

FAT_SO_OUTPUT: str = (
    "/p/x.so (architecture x86_64):\n"
    "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
    "/p/x.so (architecture arm64):\n"
    "\t/opt/homebrew/opt/chromaprint/lib/libchromaprint.1.dylib (compatibility version 1.0.0)\n"
)


@pytest.mark.requirement("INSTALL-10")
def test_a_dylibs_own_install_id_is_not_counted_as_a_dependency() -> None:
    """The first otool entry for a dylib is its id; nothing loads it by that."""
    assert parse_otool(DYLIB_OUTPUT, is_dylib=True) == ["/usr/lib/libSystem.B.dylib"]


@pytest.mark.requirement("INSTALL-10")
def test_a_bundle_has_no_id_line_so_every_entry_counts() -> None:
    assert parse_otool(FAT_SO_OUTPUT, is_dylib=False) == [
        "/usr/lib/libSystem.B.dylib",
        "/opt/homebrew/opt/chromaprint/lib/libchromaprint.1.dylib",
    ]


@pytest.mark.requirement("INSTALL-10")
def test_homebrew_links_are_violations() -> None:
    violations = link_violations(
        "pylib/x.so", parse_otool(FAT_SO_OUTPUT, is_dylib=False)
    )
    assert [v.dependency for v in violations] == [
        "/opt/homebrew/opt/chromaprint/lib/libchromaprint.1.dylib"
    ]
    assert "pylib/x.so links /opt/homebrew" in violations[0].describe()


@pytest.mark.requirement("INSTALL-10")
@pytest.mark.parametrize(
    "dependency",
    [
        "@rpath/libpython3.14.dylib",
        "@loader_path/../lib/libfoo.dylib",
        "@executable_path/../lib/libbar.dylib",
        "/usr/lib/libSystem.B.dylib",
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
    ],
)
def test_bundle_relative_and_os_paths_are_allowed(dependency: str) -> None:
    assert link_violations("x.so", [dependency]) == []


@pytest.mark.requirement("INSTALL-10")
@pytest.mark.parametrize(
    "dependency",
    [
        "/opt/homebrew/lib/libfoo.dylib",
        "/usr/local/lib/libfoo.dylib",
        "/Users/someone/.local/share/uv/python/lib/libpython3.14.dylib",
    ],
)
def test_machine_specific_absolute_paths_are_violations(dependency: str) -> None:
    assert len(link_violations("x.so", [dependency])) == 1


# ----- runtime-loaded libraries ------------------------------------------
@pytest.mark.requirement("INSTALL-11")
def test_a_find_library_site_is_found_and_named(tmp_path: Path) -> None:
    module = tmp_path / "vendor/thing.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "import ctypes.util\n"
        "lib = ctypes.util.find_library('chromaprint')\n",
        encoding="utf-8",
    )
    sites = find_runtime_load_sites(tmp_path, tmp_path)
    assert [(s.call, s.library, s.line) for s in sites] == [
        ("find_library", "chromaprint", 2)
    ]


@pytest.mark.requirement("INSTALL-11")
def test_an_unclassified_library_is_not_silently_accepted(tmp_path: Path) -> None:
    site = RuntimeLoadSite(
        path="pylib/chromaprint.py",
        line=34,
        call="find_library",
        library="chromaprint",
        source="name = ctypes.util.find_library(name)",
    )
    classified, unclassified = classify_runtime_load_sites([site])
    assert classified == {}
    assert unclassified == [site]


@pytest.mark.requirement("INSTALL-11")
def test_an_os_library_is_classified_with_a_recorded_reason() -> None:
    site = RuntimeLoadSite(
        path="pylib/vendor/x.py", line=1, call="CDLL", library="c", source="CDLL('c')"
    )
    classified, unclassified = classify_runtime_load_sites([site])
    assert unclassified == []
    assert "macOS" in classified[site.describe()]


@pytest.mark.requirement("INSTALL-11")
def test_a_dynamic_argument_is_still_required_to_be_classified() -> None:
    """"We could not read it" and "it is fine" must not render the same."""
    unknown = RuntimeLoadSite(
        path="pylib/mystery/loader.py",
        line=9,
        call="CDLL",
        library=None,
        source="ctypes.CDLL(resolve())",
    )
    known = RuntimeLoadSite(
        path="runtime/lib/python3.14/ctypes/util.py",
        line=9,
        call="CDLL",
        library=None,
        source="ctypes.CDLL(name)",
    )
    _, unclassified = classify_runtime_load_sites([unknown, known])
    assert unclassified == [unknown]


# ----- dependency exclusions ---------------------------------------------
LOCK_SAMPLE: str = """
# autogenerated
audioread==3.1.0
    # via pyacoustid
fastapi==0.136.3
    # via music-dj-tools
pyacoustid==1.3.1
    # via music-dj-tools
requests==2.34.2
    # via pyacoustid
standard-aifc==3.13.0 ; python_full_version >= '3.13'
    # via audioread
starlette==1.2.1
    # via fastapi
idna==3.18
    # via
    #   anyio
    #   requests
anyio==4.13.0
    # via starlette
"""


@pytest.mark.requirement("INSTALL-12")
def test_the_lock_is_parsed_with_its_via_edges() -> None:
    entries = {entry.name: entry for entry in parse_locked_export(LOCK_SAMPLE)}
    assert entries["fastapi"].spec == "fastapi==0.136.3"
    assert entries["fastapi"].via == frozenset({"music-dj-tools"})
    assert entries["idna"].via == frozenset({"anyio", "requests"})
    assert "python_full_version" in entries["standard-aifc"].spec


@pytest.mark.requirement("INSTALL-12")
def test_excluding_a_dependency_drops_what_only_it_needed() -> None:
    kept, dropped = prune_excluded(parse_locked_export(LOCK_SAMPLE), "music-dj-tools")
    assert dropped == ["audioread", "pyacoustid", "requests", "standard-aifc"]
    assert {entry.name for entry in kept} == {
        "fastapi",
        "starlette",
        "idna",
        "anyio",
    }


@pytest.mark.requirement("INSTALL-12")
def test_a_shared_transitive_dependency_survives_the_exclusion() -> None:
    """idna is reached through anyio too, so dropping requests must keep it."""
    _, dropped = prune_excluded(parse_locked_export(LOCK_SAMPLE), "music-dj-tools")
    assert "idna" not in dropped


@pytest.mark.requirement("INSTALL-12")
def test_a_stale_exclusion_is_an_error_not_a_no_op() -> None:
    lock = "fastapi==0.136.3\n    # via music-dj-tools\n"
    with pytest.raises(PayloadBuildError) as excinfo:
        prune_excluded(parse_locked_export(lock), "music-dj-tools")
    assert "pyacoustid" in str(excinfo.value)


@pytest.mark.requirement("INSTALL-12")
def test_every_exclusion_states_why_it_is_safe() -> None:
    for name, reason in EXCLUDED_DEPENDENCIES.items():
        assert len(reason) > 80, f"{name} has no real justification recorded"


# ----- identity -----------------------------------------------------------
def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-b", "trunk"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "first"], cwd=path, check=True, capture_output=True
    )


@pytest.mark.requirement("INSTALL-07")
def test_a_clean_tree_stamps_clean(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    identity = git_identity(tmp_path)
    assert identity["git_dirty"] is False
    assert identity["git_branch"] == "trunk"
    assert identity["git_sha"] == identity["git_sha_full"][:8]


@pytest.mark.requirement("INSTALL-07")
def test_a_dirty_tree_stamps_dirty(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")
    assert git_identity(tmp_path)["git_dirty"] is True


@pytest.mark.requirement("INSTALL-07")
def test_a_directory_git_cannot_describe_fails_the_build(tmp_path: Path) -> None:
    with pytest.raises(PayloadBuildError):
        git_identity(tmp_path)


@pytest.mark.requirement("INSTALL-07")
def test_the_manifest_carries_every_key_build_info_demands() -> None:
    """The two halves of the identity contract, checked against each other.

    build_info refuses a manifest missing any of these, so a builder that
    stopped writing one would ship a payload whose own identity endpoint
    answers 503.
    """
    stamped = set(git_identity(REPO_ROOT)) | {
        "built_at_utc",
        "lane_label",
        "product_name",
        "bundle_identifier",
        "app_version",
        "engine_version",
    }
    assert set(REQUIRED_IDENTITY_KEYS) <= stamped


# ----- digests ------------------------------------------------------------
def test_the_tree_digest_notices_a_rename(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("same", encoding="utf-8")
    before = sha256_tree(tmp_path)
    (tmp_path / "one.txt").rename(tmp_path / "two.txt")
    assert sha256_tree(tmp_path) != before


def test_the_tree_digest_is_stable_for_identical_content(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a/x.txt").write_text("x", encoding="utf-8")
    (tmp_path / "b/x.txt").write_text("x", encoding="utf-8")
    assert sha256_tree(tmp_path / "a") == sha256_tree(tmp_path / "b")


# ----- the shipped configuration -----------------------------------------
@pytest.mark.requirement("INSTALL-13")
def test_the_launcher_unsets_rekordbox_writeback() -> None:
    """One-way safety is absolute: the bundle removes any inherited opt-in."""
    from scripts.build_engine_payload import LAUNCHER_TEMPLATE

    assert "unset MDT_REKORDBOX_WRITEBACK_ENABLED" in LAUNCHER_TEMPLATE


@pytest.mark.requirement("INSTALL-13")
def test_the_launcher_never_writes_bytecode_into_a_read_only_bundle() -> None:
    from scripts.build_engine_payload import LAUNCHER_TEMPLATE

    assert "PYTHONDONTWRITEBYTECODE=1" in LAUNCHER_TEMPLATE
    assert "PYTHONNOUSERSITE=1" in LAUNCHER_TEMPLATE


def test_locked_requirement_is_hashable_for_set_arithmetic() -> None:
    entry = LockedRequirement(name="a", spec="a==1", via=frozenset({"b"}))
    assert {entry, entry} == {entry}


# ----- the copy must not eat its own output ------------------------------
@pytest.mark.requirement("INSTALL-12")
def test_the_app_copy_skips_the_payload_it_is_writing(tmp_path: Path) -> None:
    """The staging dir lives under apps/, which is what the build copies.

    Found the expensive way: the first dmg run walked
    apps/desktop/src-tauri/payload/app/apps/desktop/src-tauri/payload/... until
    macOS refused the path length.
    """
    output_root = tmp_path / "src-tauri" / "payload"
    output_root.mkdir(parents=True)
    ignore = skip_output_tree(output_root)
    skipped = ignore(str(output_root.parent), ["payload", "src", "Cargo.toml"])
    assert "payload" in skipped
    assert "src" not in skipped and "Cargo.toml" not in skipped


@pytest.mark.requirement("INSTALL-12")
def test_the_copy_filter_matches_the_path_not_the_name(tmp_path: Path) -> None:
    """An unrelated directory called payload must still be copied."""
    output_root = tmp_path / "src-tauri" / "payload"
    output_root.mkdir(parents=True)
    unrelated = tmp_path / "apps" / "engine_core"
    unrelated.mkdir(parents=True)
    (unrelated / "payload").mkdir()
    ignore = skip_output_tree(output_root)
    assert "payload" not in ignore(str(unrelated), ["payload"])


@pytest.mark.requirement("INSTALL-12")
def test_the_copy_filter_still_drops_the_heavy_derived_trees(tmp_path: Path) -> None:
    output_root = tmp_path / "payload"
    output_root.mkdir()
    ignore = skip_output_tree(output_root)
    skipped = ignore(
        str(tmp_path),
        ["node_modules", "target", ".svelte-kit", "__pycache__", "test-results", "src"],
    )
    assert skipped == {
        "node_modules",
        "target",
        ".svelte-kit",
        "__pycache__",
        "test-results",
    }
