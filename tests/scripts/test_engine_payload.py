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

import shutil
import subprocess
from pathlib import Path

import pytest

from apps.engine_core.build_info import REQUIRED_IDENTITY_KEYS
from scripts.build_engine_payload import (
    EXCLUDED_DEPENDENCIES,
    LockedRequirement,
    PayloadBuildError,
    RuntimeLoadSite,
    assert_installed_is_locked,
    assert_spa_is_fresh,
    assert_verify_report,
    classify_runtime_load_sites,
    find_runtime_load_sites,
    git_identity,
    install_waveform_native,
    installed_distributions,
    link_violations,
    parse_locked_export,
    parse_otool,
    prune_excluded,
    sha256_tree,
    skip_output_tree,
    sole_stretch_asset,
    sole_waveform_wheel,
)
from scripts.desktop_lane_config import LaneLabelError, validate_label

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


# ----- lane label: optional since OPS-08, unsafe still refused -----------
@pytest.mark.requirement("OPS-08")
@pytest.mark.parametrize("raw", [None, "", "   "])
def test_absent_lane_label_builds_the_plain_product(raw: str | None) -> None:
    """The bake-off is over: unset means Open DJ / com.opendj.desktop."""
    assert validate_label(raw) is None


@pytest.mark.requirement("INSTALL-06")
def test_a_present_label_is_returned_unchanged() -> None:
    assert validate_label("  B  ") == "B"


@pytest.mark.requirement("INSTALL-06")
def test_an_unsafe_label_is_still_refused_not_defaulted() -> None:
    with pytest.raises(LaneLabelError):
        validate_label("lane b")


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
def test_the_linux_only_libc_soname_is_classified_not_bundled() -> None:
    """[if] parent_watch's CDLL("libc.so.6") is unclassified [then] every dmg build fails."""
    site = RuntimeLoadSite(
        path="app/apps/engine_core/parent_watch.py",
        line=55,
        call="CDLL",
        library="libc.so.6",
        source='CDLL("libc.so.6")',
    )
    classified, unclassified = classify_runtime_load_sites([site])
    assert unclassified == []
    assert "linux" in classified[site.describe()]


@pytest.mark.requirement("INSTALL-11")
@pytest.mark.parametrize(
    "line,call,source",
    [
        (40, "find_library", "path = find_library(name)"),
        (43, "CDLL", "return CDLL(path, use_errno=True)"),
    ],
)
def test_truststore_macos_framework_loads_are_classified(line: int, call: str, source: str) -> None:
    """[if] truststore's two dynamic loads (pulled in by mcp 2.x via httpx2) are
    unclassified [then] every dmg build from main fails, as the Air's did Tue 15 Sep."""
    site = RuntimeLoadSite(
        path="pylib/truststore/_macos.py", line=line, call=call, library=None, source=source
    )
    classified, unclassified = classify_runtime_load_sites([site])
    assert unclassified == []
    assert "/System/Library/Frameworks" in classified[site.describe()]


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


@pytest.mark.requirement("INSTALL-11")
def test_allowlist_matches_site_key_for_dynamic_path_line_entries() -> None:
    """[if] dynamic site key is path:line [then] allowlist lookup classifies it."""
    site = RuntimeLoadSite(
        path="pylib/torch/_ops.py",
        line=1350,
        call="CDLL",
        library=None,
        source="ctypes.CDLL(path)",
    )
    classified, unclassified = classify_runtime_load_sites([site])
    assert unclassified == []
    assert "package-relative" in classified[site.describe()]


@pytest.mark.requirement("INSTALL-11")
@pytest.mark.parametrize(
    "library,keyword",
    [
        ("kernel32", "Windows"),
        ("kernel32.dll", "win32"),
        ("vcruntime140.dll", "Windows"),
        ("msvcp140.dll", "Windows"),
        ("vcruntime140_1.dll", "Windows"),
        ("libc.so", "Linux"),
        ("/usr/lib64/libgomp.so.1", "Linux"),
        ("libnvidia-ml.so.1", "CUDA"),
        ("__lib_path__", "darwin"),
    ],
)
def test_torch_filelock_literal_runtime_loads_are_classified(
    library: str, keyword: str
) -> None:
    """[if] torch/filelock literal is unclassified [then] dmg payload guard fails."""
    site = RuntimeLoadSite(
        path="pylib/torch/example.py",
        line=1,
        call="CDLL",
        library=library,
        source=f'CDLL("{library}")',
    )
    classified, unclassified = classify_runtime_load_sites([site])
    assert unclassified == []
    assert keyword in classified[site.describe()]


@pytest.mark.requirement("INSTALL-11")
def test_filelock_cdll_none_is_classified() -> None:
    """[if] filelock CDLL(None) is unclassified [then] dmg payload guard fails."""
    site = RuntimeLoadSite(
        path="pylib/filelock/_identity.py",
        line=162,
        call="CDLL",
        library=None,
        source="_LIBC: Final[ctypes.CDLL] = ctypes.CDLL(None, use_errno=True)",
    )
    classified, unclassified = classify_runtime_load_sites([site])
    assert unclassified == []
    assert "dlopen(NULL)" in classified[site.describe()]


@pytest.mark.requirement("INSTALL-11")
def test_an_unlisted_torch_runtime_load_still_fails() -> None:
    """[if] a new torch ctypes site is unlisted [then] guard still fails closed."""
    site = RuntimeLoadSite(
        path="pylib/torch/_future_probe.py",
        line=1,
        call="CDLL",
        library="unknown_cuda_thing",
        source='CDLL("unknown_cuda_thing")',
    )
    classified, unclassified = classify_runtime_load_sites([site])
    assert classified == {}
    assert unclassified == [site]


@pytest.mark.requirement("INSTALL-11")
def test_all_torch_filelock_runtime_sites_in_installed_wheel_are_classified(
    tmp_path: Path,
) -> None:
    """[if] any torch/filelock site in the wheel stays unclassified [then] guard fails."""
    try:
        import filelock
        import torch
    except ImportError:
        pytest.skip("torch/filelock not installed; sync --extra vocals to run")

    payload_dir = tmp_path / "payload"
    pylib = payload_dir / "pylib"
    pylib.mkdir(parents=True)

    torch_src = Path(torch.__file__).parent
    filelock_src = Path(filelock.__file__).parent
    rel_paths = [
        "filelock/_identity.py",
        "torch/__init__.py",
        "torch/_inductor/codecache.py",
        "torch/_inductor/cpp_builder.py",
        "torch/_inductor/cpu_vec_isa.py",
        "torch/_ops.py",
        "torch/cuda/__init__.py",
        "torch/cuda/memory.py",
    ]
    for rel in rel_paths:
        src = filelock_src / rel.split("/", 1)[1] if rel.startswith("filelock") else torch_src / rel.split("/", 1)[1]
        dst = pylib / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    sites = find_runtime_load_sites(payload_dir, payload_dir)
    _, unclassified = classify_runtime_load_sites(sites)
    assert unclassified == [], (
        "unclassified torch/filelock sites:\n"
        + "\n".join(f"  {s.describe()}" for s in unclassified)
    )


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
starlette==1.3.1
    # via fastapi
idna==3.18
    # via
    #   anyio
    #   requests
anyio==4.13.0
    # via starlette
cryptography==46.0.7
    # via music-dj-tools
cffi==2.0.0 ; platform_python_implementation != 'PyPy'
    # via cryptography
pycparser==3.0 ; implementation_name != 'PyPy'
    # via cffi
librosa==0.10.2.post1
    # via music-dj-tools
scipy==1.17.1
    # via
    #   librosa
    #   music-dj-tools
soundfile==0.13.1
    # via
    #   librosa
    #   music-dj-tools
scikit-learn==1.9.0
    # via librosa
demucs==4.0.1
    # via music-dj-tools
torch==2.5.1
    # via music-dj-tools
torchaudio==2.5.1
    # via music-dj-tools
sentry-sdk==2.66.1
    # via music-dj-tools
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
        "cryptography",
        "cffi",
        "pycparser",
        # the requested "analysis" extra and what only it pulls in; audioread
        # is still dropped because pyacoustid, not librosa, is its via edge
        # in this sample.
        "librosa",
        "scipy",
        "soundfile",
        "scikit-learn",
        "demucs",
        "torch",
        "torchaudio",
        # the requested "observability" extra (OBS-04): the dmg ships the SDK
        "sentry-sdk",
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



def _pylib_with(tmp_path: Path, *dist_infos: str) -> Path:
    pylib = tmp_path / "pylib"
    for name in dist_infos:
        (pylib / f"{name}.dist-info").mkdir(parents=True)
    return pylib


@pytest.mark.requirement("INSTALL-12")
def test_installed_distributions_normalizes_names_like_the_lock(tmp_path: Path) -> None:
    pylib = _pylib_with(tmp_path, "annotated_doc-0.0.4", "cffi-2.0.0")
    assert installed_distributions(pylib) == {"annotated-doc": "0.0.4", "cffi": "2.0.0"}


@pytest.mark.requirement("INSTALL-12")
def test_an_exact_locked_install_passes_even_with_marker_skipped_packages(
    tmp_path: Path,
) -> None:
    """standard-aifc is locked behind a 3.13 marker; its absence is not a stray."""
    pylib = _pylib_with(tmp_path, "fastapi-0.136.3", "idna-3.18")
    locked = [
        entry
        for entry in parse_locked_export(LOCK_SAMPLE)
        if entry.name in {"fastapi", "idna", "standard-aifc"}
    ]
    assert_installed_is_locked(pylib, locked)


@pytest.mark.requirement("INSTALL-12")
def test_a_package_uv_resolved_past_the_lock_is_a_stray(tmp_path: Path) -> None:
    """The Fri 11 Sep 2026 finding: without --no-deps uv installed cffi 2.1.1
    from cryptography's wheel metadata while the lock pins 2.0.0."""
    pylib = _pylib_with(tmp_path, "cryptography-46.0.7", "cffi-2.1.1")
    kept, _ = prune_excluded(parse_locked_export(LOCK_SAMPLE), "music-dj-tools")
    with pytest.raises(PayloadBuildError) as excinfo:
        assert_installed_is_locked(pylib, kept)
    assert "cffi==2.1.1" in str(excinfo.value)


@pytest.mark.requirement("INSTALL-12")
def test_a_package_the_lock_never_names_is_a_stray(tmp_path: Path) -> None:
    pylib = _pylib_with(tmp_path, "fastapi-0.136.3", "leftpad-1.0.0")
    with pytest.raises(PayloadBuildError) as excinfo:
        assert_installed_is_locked(pylib, parse_locked_export(LOCK_SAMPLE))
    assert "leftpad==1.0.0" in str(excinfo.value)


@pytest.mark.requirement("INSTALL-12")
def test_a_locked_package_at_an_unlocked_version_is_a_stray(tmp_path: Path) -> None:
    pylib = _pylib_with(tmp_path, "fastapi-0.137.0")
    with pytest.raises(PayloadBuildError) as excinfo:
        assert_installed_is_locked(pylib, parse_locked_export(LOCK_SAMPLE))
    assert "fastapi==0.137.0" in str(excinfo.value)


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


def test_the_launcher_keeps_the_callers_cwd_off_sys_path() -> None:
    """python -m prepends the caller's cwd; a repo checkout as cwd must never
    shadow the payload (a repo-built _rb_waveform_native.so did exactly that
    on the Air, Sun 31 Aug 2026, masking the shipped backend)."""
    from scripts.build_engine_payload import LAUNCHER_TEMPLATE

    assert "PYTHONSAFEPATH=1" in LAUNCHER_TEMPLATE


def test_locked_requirement_is_hashable_for_set_arithmetic() -> None:
    entry = LockedRequirement(name="a", spec="a==1", via=frozenset({"b"}))
    assert {entry, entry} == {entry}


# ----- native waveform staging -------------------------------------------
def _verify_report(**overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "routes": 110,
        "missing": [],
        "frontend_build_dir": "/payload/app/apps/webui/frontend/build",
        "frontend_build_exists": True,
        "waveform": {
            "requested": "auto",
            "selected": "rust-pyo3",
            "native_available": True,
            "native_import_error": None,
        },
        "crypto": {"has_crypto": True, "rust_bindings_error": None},
        "analysis": {
            "backend": "librosa",
            "modules": ["librosa", "scipy"],
            "installed": True,
            "import_error": None,
            "auto_analyze_arms": True,
        },
        "telemetry": {"sdk_version": "2.66.1", "sdk_import_error": None},
    }
    report.update(overrides)
    return report


def test_a_payload_that_cannot_import_sentry_sdk_stops_the_build() -> None:
    """OBS-04: the bundled DSN is decoration without the SDK, and the payload
    omitted the SDK for its whole life before Mon 21 Sep 2026, so this is
    the regression that actually shipped."""
    with pytest.raises(PayloadBuildError, match="cannot import sentry_sdk"):
        assert_verify_report(
            _verify_report(
                telemetry={
                    "sdk_version": None,
                    "sdk_import_error": "ModuleNotFoundError: No module named 'sentry_sdk'",
                }
            )
        )


def test_a_healthy_verify_report_passes() -> None:
    assert_verify_report(_verify_report())


def test_a_payload_on_the_numpy_fallback_stops_the_build() -> None:
    """The exact regression shipped in the Sat 29 Aug lane-B dmg: auto policy
    fell back silently and every waveform materialized on the slow path."""
    with pytest.raises(PayloadBuildError, match="NumPy waveform fallback"):
        assert_verify_report(
            _verify_report(
                waveform={
                    "requested": "auto",
                    "selected": "python-numpy",
                    "native_available": False,
                    "native_import_error": "ModuleNotFoundError: ...",
                }
            )
        )


def test_a_payload_without_the_analysis_backend_stops_the_build() -> None:
    """Measured on the shipped build on the test Mac Wed 16 Sep 2026: librosa
    was absent, so analyze-on-import never armed and a folder import left all
    100 tracks with no BPM, no key and no beatgrid, permanently."""
    with pytest.raises(PayloadBuildError, match="no BPM, no key and no beatgrid"):
        assert_verify_report(
            _verify_report(
                analysis={
                    "backend": "librosa",
                    "modules": ["librosa", "scipy"],
                    "installed": False,
                    "import_error": None,
                    "auto_analyze_arms": False,
                }
            )
        )


def test_a_payload_whose_analysis_backend_fails_to_import_stops_the_build() -> None:
    """find_spec saying yes and the backend class importing are different
    questions, and the drain asks the second one. A closure that installs
    librosa against an incompatible numpy passes the first and fails here."""
    with pytest.raises(PayloadBuildError, match="cannot load"):
        assert_verify_report(
            _verify_report(
                analysis={
                    "backend": "librosa",
                    "modules": ["librosa", "scipy"],
                    "installed": True,
                    "import_error": "ImportError: numpy.core.multiarray failed",
                    "auto_analyze_arms": True,
                }
            )
        )


def test_a_payload_whose_auto_analyze_declines_to_arm_stops_the_build() -> None:
    """The opposite mutation to the two above: the backend imports fine but
    the loop that would USE it stays disarmed, which is the state that looks
    healthiest and analyzes exactly as much as a missing backend does."""
    with pytest.raises(PayloadBuildError, match="analyze-on-import"):
        assert_verify_report(
            _verify_report(
                analysis={
                    "backend": "librosa",
                    "modules": ["librosa", "scipy"],
                    "installed": True,
                    "import_error": None,
                    "auto_analyze_arms": False,
                }
            )
        )


def test_a_payload_whose_crypto_bindings_do_not_load_stops_the_build() -> None:
    """Fri 11 Sep 2026: cffi pruned, cryptography's _rust import failed, pyjwt
    swallowed it, and the route-table boot still passed with RS256 dead."""
    with pytest.raises(PayloadBuildError, match="has_crypto=False"):
        assert_verify_report(
            _verify_report(
                crypto={
                    "has_crypto": False,
                    "rust_bindings_error": "ModuleNotFoundError: No module named '_cffi_backend'",
                }
            )
        )


def test_missing_routes_still_stop_the_build() -> None:
    with pytest.raises(PayloadBuildError, match="missing routes"):
        assert_verify_report(_verify_report(missing=["/api/v1/health"]))


def test_zero_wheels_is_an_error_not_a_random_pick(tmp_path: Path) -> None:
    with pytest.raises(PayloadBuildError, match="exactly one waveform wheel"):
        sole_waveform_wheel(tmp_path)


def test_two_wheels_is_an_error_not_a_random_pick(tmp_path: Path) -> None:
    (tmp_path / "a-1.0-cp311-abi3-macosx_11_0_arm64.whl").write_bytes(b"")
    (tmp_path / "b-1.0-cp311-abi3-macosx_11_0_arm64.whl").write_bytes(b"")
    with pytest.raises(PayloadBuildError, match="exactly one waveform wheel"):
        sole_waveform_wheel(tmp_path)


def test_one_wheel_is_returned(tmp_path: Path) -> None:
    wheel = tmp_path / "only-1.0-cp311-abi3-macosx_11_0_arm64.whl"
    wheel.write_bytes(b"")
    assert sole_waveform_wheel(tmp_path) == wheel


def test_a_closure_without_numpy_stops_the_waveform_install(tmp_path: Path) -> None:
    """--no-deps means the closure must already carry numpy; checked, not assumed."""
    with pytest.raises(PayloadBuildError, match="no longer carries numpy"):
        install_waveform_native(
            tmp_path / "w.whl", tmp_path / "python3", tmp_path, ["fastapi==0.136.3"]
        )


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
