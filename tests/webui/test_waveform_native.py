"""Parity coverage for the optional PyO3 ANLZ waveform materializer.

The arrays are decoded from the repository's captured real Rekordbox ANLZ
fixture through rb_vendor's production parser. Set MDT_REQUIRE_WAVEFORM_NATIVE=1
after a release build to make native availability fail closed for acceptance.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from apps.shared.hashing import sha256_file
from apps.webui.server import rb_vendor
from tests.fixtures._resolver import FIXTURES_ROOT
from tests.fixtures.conftest import resolve_required_fixture

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCKED_FIXTURE_FILES = frozenset({"ANLZ0000.DAT", "ANLZ0000.EXT", "ANLZ0000.2EX"})

# Committed directly under tests/fixtures/ (a sibling of the rb-usb-export.extern
# marker), NOT inside tests/fixtures/rb-usb-export/ itself: that directory is
# what leaves the repo in the history rewrite, and once the payload resolves
# through a mutable external host, a manifest living alongside it could be
# swapped in lockstep with a stale/regenerated payload and the checksum
# assertion below would pass without proving canonical content.
LOCKED_FIXTURE_MANIFEST = FIXTURES_ROOT / "rb-usb-export.manifest.json"


def _rb_usb_export_root() -> Path:
    """Resolve ``rb-usb-export`` through the resolver, wherever it lives.

    Routes through ``resolve_required_fixture()`` (rather than a hard-coded
    repo path) so this test keeps working once the in-repo directory leaves
    and only ``rb-usb-export.extern`` remains (PR #718). This CAT-05
    acceptance test fails closed unless the caller explicitly opts out via
    MDT_ALLOW_MISSING_FIXTURES=1 -- unrelated to MDT_REQUIRE_WAVEFORM_NATIVE,
    which gates native-extension availability in ``_native()``, not fixture
    availability.
    """
    return resolve_required_fixture("rb-usb-export")


def _locked_fixture_manifest() -> dict[str, Any]:
    manifest = json.loads(LOCKED_FIXTURE_MANIFEST.read_text())
    assert manifest["contract_version"] == 1
    assert manifest["fixture"] == "PIONEER/USBANLZ/P000/00029138"
    assert set(manifest["files"]) == LOCKED_FIXTURE_FILES
    return manifest


def _locked_fixture_dir() -> Path:
    return _rb_usb_export_root() / "PIONEER" / "USBANLZ" / "P000" / "00029138"


def _verify_locked_fixture() -> dict[str, str]:
    manifest = _locked_fixture_manifest()
    fixture_dir = _locked_fixture_dir()
    actual = {name: sha256_file(fixture_dir / name) for name in sorted(LOCKED_FIXTURE_FILES)}
    assert actual == manifest["files"], "canonical waveform fixture checksum mismatch"
    return actual


def _fixture_dir() -> Path:
    external = os.environ.get("MDT_WAVEFORM_ANLZ_DIR")
    if os.environ.get("MDT_REQUIRE_WAVEFORM_NATIVE") == "1" and external is not None:
        raise RuntimeError(
            "MDT_WAVEFORM_ANLZ_DIR is forbidden when "
            "MDT_REQUIRE_WAVEFORM_NATIVE=1; release evidence must use the locked fixture"
        )
    if external is not None:
        return Path(external).expanduser()
    _verify_locked_fixture()
    return _locked_fixture_dir()


def _native() -> Any:
    native = rb_vendor._WAVEFORM_NATIVE
    if native is None:
        message = (
            "release waveform extension unavailable; build with maturin or unset "
            "MDT_REQUIRE_WAVEFORM_NATIVE outside the native acceptance gate"
        )
        if os.environ.get("MDT_REQUIRE_WAVEFORM_NATIVE") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return native


def _real_bands(tag_name: str) -> dict[str, np.ndarray]:
    tags, unreadable = rb_vendor._first_tags(_fixture_dir())
    assert unreadable == []
    assert tag_name in tags
    bands = rb_vendor._tri_bands(tags[tag_name])
    assert all(array.ndim == 1 for array in bands.values())
    assert all(array.dtype == np.float64 for array in bands.values())
    return bands


def _copy_tracked_apps(destination: Path) -> None:
    """Hydrate a source-only package tree without editable-build artifacts."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "apps"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.split("\0")
    for relative in filter(None, tracked):
        source = REPO_ROOT / relative
        if not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _backend_subprocess(mode: str, *, native_visible: bool) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MDT_WAVEFORM_BACKEND"] = mode
    if native_visible:
        env["PYTHONPATH"] = os.pathsep.join(str(path) for path in sys.path if path)
    interpreter = [sys.executable]
    if not native_visible:
        # Exercise a genuinely clean dependency environment rather than
        # blocking or monkeypatching the extension import. This remains valid
        # even after the release gate installs the production wheel locally.
        interpreter = [
            "uv",
            "run",
            "--isolated",
            "--no-project",
            "--python",
            f"{sys.version_info.major}.{sys.version_info.minor}",
            "--with",
            "numpy<2",
            "--with",
            "fastapi>=0.115",
            "--with",
            "pyrekordbox>=0.4.4",
            "python",
        ]
    command = [
        *interpreter,
        "-c",
        "import json; from apps.webui.server import rb_vendor; "
        "print(rb_vendor.waveform_materialization_backend_request()); "
        "print(rb_vendor.waveform_materialization_backend()); "
        "print(json.dumps(rb_vendor.waveform_materialization_status(), sort_keys=True))",
    ]
    if native_visible:
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    # An editable setuptools-rust build may place the generated extension in
    # the repository root (not site-packages), especially on Windows. Run the
    # real tracked Python package from a disposable source-only tree so neither
    # cwd nor PYTHONPATH can discover that build artifact.
    with tempfile.TemporaryDirectory(prefix="waveform-python-backend-") as temp:
        isolated_root = Path(temp)
        _copy_tracked_apps(isolated_root)
        env["PYTHONPATH"] = str(isolated_root)
        return subprocess.run(
            command,
            cwd=isolated_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )


def _external_fixture_probe(*, require_native: bool) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MDT_REQUIRE_WAVEFORM_NATIVE"] = "1" if require_native else "0"
    env["MDT_WAVEFORM_ANLZ_DIR"] = str(_locked_fixture_dir())
    env["MDT_WAVEFORM_BACKEND"] = "python"
    command = (
        "import runpy; "
        f"scope = runpy.run_path({str(Path(__file__).resolve())!r}); "
        "assert scope['_real_bands']('PWV7')"
    )
    return subprocess.run(
        [sys.executable, "-c", command],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_backend_boundary_is_inspectable_and_dispatches() -> None:
    bands = {"low": np.array([0.0, 0.5, 1.0], dtype=np.float64)}
    backend = rb_vendor.waveform_materialization_backend()
    assert backend in {"python-numpy", "rust-pyo3"}
    assert rb_vendor._bands_payload(bands, 100) == rb_vendor._bands_payload_python(bands, 100)
    assert rb_vendor.waveform_materialization_backend_request() in {
        "auto",
        "python",
        "native",
    }


def test_auto_uses_python_when_native_is_unavailable() -> None:
    completed = _backend_subprocess("auto", native_visible=False)
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.strip().splitlines()
    assert lines[:2] == ["auto", "python-numpy"]
    status = json.loads(lines[2])
    assert status["selected"] == "python-numpy"
    assert status["native_available"] is False
    assert status["native_import_error"].endswith(
        "No module named '_rb_waveform_native'"
    )
    assert "using Python/NumPy fallback" in completed.stderr


def test_python_explicitly_forces_fallback() -> None:
    completed = _backend_subprocess("python", native_visible=True)
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.strip().splitlines()
    assert lines[:2] == ["python", "python-numpy"]
    status = json.loads(lines[2])
    assert status["native_import_error"] is None


def test_native_request_fails_explicitly_when_extension_is_unavailable() -> None:
    completed = _backend_subprocess("native", native_visible=False)
    assert completed.returncode != 0
    assert "MDT_WAVEFORM_BACKEND=native requested" in completed.stderr


def test_invalid_backend_request_fails_at_import() -> None:
    completed = _backend_subprocess("rust", native_visible=False)
    assert completed.returncode != 0
    assert "MDT_WAVEFORM_BACKEND must be one of auto, python, or native" in completed.stderr


def test_native_request_selects_rust_when_extension_is_available() -> None:
    _native()
    completed = _backend_subprocess("native", native_visible=True)
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.strip().splitlines()
    assert lines[:2] == ["native", "rust-pyo3"]
    status = json.loads(lines[2])
    assert status["native_available"] is True
    assert status["native_import_error"] is None


def test_collision_resistant_extension_name() -> None:
    native = _native()
    assert native.__name__ == "_rb_waveform_native"


def test_canonical_fixture_matches_locked_manifest() -> None:
    assert _verify_locked_fixture() == _locked_fixture_manifest()["files"]


def test_release_acceptance_rejects_external_fixture_override() -> None:
    completed = _external_fixture_probe(require_native=True)
    assert completed.returncode != 0
    assert "release evidence must use the locked fixture" in completed.stderr


def test_nonrelease_external_fixture_override_is_explicitly_allowed() -> None:
    completed = _external_fixture_probe(require_native=False)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(("tag_name", "points"), [("PWV6", 100), ("PWV7", 38400)])
def test_native_exactly_matches_python_on_real_anlz_arrays(tag_name: str, points: int) -> None:
    bands = _real_bands(tag_name)
    assert all(not array.flags.c_contiguous for array in bands.values())

    expected = rb_vendor._bands_payload_python(bands, points)
    actual = _native().bands_payload(bands, points)

    assert actual == expected
    assert actual["length"] == min(len(next(iter(bands.values()))), points)


def test_native_matches_empty_no_downsample_and_zero_point_edges() -> None:
    native = _native()
    cases = [
        ({"low": np.array([], dtype=np.float64)}, 100),
        ({"low": np.array([0.0, 1.0 / 127, 1.0], dtype=np.float64)}, 100),
        ({"low": np.arange(128, dtype=np.float64) / 127.0}, 0),
        ({"low": np.arange(128, dtype=np.float64) / 127.0}, -1),
    ]
    for bands, points in cases:
        assert native.bands_payload(bands, points) == rb_vendor._bands_payload_python(bands, points)


def test_production_dispatch_rejects_mismatched_band_lengths() -> None:
    bands = {
        "low": np.zeros(4, dtype=np.float64),
        "mid": np.zeros(3, dtype=np.float64),
    }
    with pytest.raises(ValueError, match="waveform bands must have equal lengths"):
        rb_vendor._bands_payload(bands, 100)
    with pytest.raises(ValueError, match="waveform bands must have equal lengths"):
        _native().bands_payload(bands, 100)


def test_dispatch_reports_and_uses_native_backend() -> None:
    bands = _real_bands("PWV7")
    native = _native()
    assert native.BACKEND == "rust-pyo3"
    assert rb_vendor.waveform_materialization_backend() == "rust-pyo3"
    assert rb_vendor._bands_payload(bands, 38400) == native.bands_payload(bands, 38400)


@pytest.mark.parametrize(
    "invalid",
    [
        np.zeros((2, 2), dtype=np.float64),
        np.zeros(4, dtype=np.float32),
    ],
)
def test_native_rejects_arrays_outside_production_boundary(invalid: np.ndarray) -> None:
    with pytest.raises(TypeError, match="NumPy 1-D float64 array"):
        _native().bands_payload({"low": invalid}, 100)
