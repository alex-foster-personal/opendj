"""The payload carries everything the beatgrid backfill producer needs offline.

- [if] a payload is built [then] it ships the beatgrid runner, launcher and checkpoint, [else stop].

NATIVE-10 (issue #2315): a v1 backfill runs on the user's machine with no
network after install, for every lane except stems and lyrics. Waveform,
loudness and key already ran from the payload's own closure; beatgrid did
not, and key needs beatgrid's own downbeats, so two of the four lanes could
not produce a record on an installed app. The producer needed three things
the payload did not carry:

- an interpreter with Beat This! and its EXACT PEP 723 pins (torch 2.14.0,
  not the vocal worker's 2.5.1 already in pylib),
- the ``final0`` checkpoint (Beat This! otherwise fetches it over the
  network on first use),
- a launcher contract naming both, because ``own_beatgrid`` refuses to guess.

These tests pin that contract on the REAL launcher templates and the REAL
staging functions. The end-to-end proof (a built payload, networking denied,
four lanes over real tracks) is ``tests/integration/test_native10_offline_backfill.py``.

Acceptance tests:

- [if] the engine launcher runs [then] MDT_BEATGRID_RUNNER_PYTHON and
  MDT_BEATGRID_WEIGHTS name payload paths, under the exact env names
  own_beatgrid and weights read, [else ⛔️].
- [if] the runner launcher runs from an engine env [then] its PYTHONPATH is
  the runner site ONLY (no app/, no pylib/), [else ⛔️].
- [if] bin/opendj-python runs [then] it hands its argv to the payload
  interpreter under the engine env contract, [else ⛔️].
- [if] the runner closure is exported [then] every ``==`` pin in the
  runner's own PEP 723 block appears at that version, [else ⛔️].
- [if] the checkpoint download or the cached file has the wrong digest
  [then] the build fails and caches nothing, [else ⛔️].
- [if] a staged payload lacks the checkpoint or the runner site [then]
  verification fails naming the missing piece, [else ⛔️].
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from apps.analysis.backends import own_beatgrid
from apps.analysis_beatgrid import weights
from scripts import payload_beatgrid
from scripts.build_engine_payload import (
    PYTHON_LAUNCHER_RELATIVE,
    PayloadBuildError,
    assert_installed_is_locked,
    parse_locked_export,
    write_launchers,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.requirement("NATIVE-10")

STUB_PYTHON = """#!/bin/sh
printf 'pythonpath=%s\\n' "$PYTHONPATH"
printf 'runner_python=%s\\n' "${MDT_BEATGRID_RUNNER_PYTHON-UNSET}"
printf 'weights=%s\\n' "${MDT_BEATGRID_WEIGHTS-UNSET}"
printf 'argv=%s\\n' "$*"
"""


@pytest.fixture
def payload(tmp_path: Path) -> Path:
    root = tmp_path / "Open DJ.app" / "Contents" / "Resources" / "payload"
    python = root / "runtime" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text(STUB_PYTHON, encoding="utf-8")
    python.chmod(0o755)
    write_launchers(root)
    payload_beatgrid.write_runner_launcher(root)
    return root


def _run(launcher: Path, *args: str, env: dict[str, str] | None = None) -> dict[str, str]:
    result = subprocess.run(
        [str(launcher), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env if env is not None else {"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, f"{launcher} failed: {result.stderr}"
    return dict(line.split("=", 1) for line in result.stdout.strip().splitlines())


# ----- launcher contract ----------------------------------------------------


def test_python_launcher_exports_beatgrid_runner_and_weights_under_the_names_the_producer_reads(
    payload: Path,
) -> None:
    report = _run(payload / PYTHON_LAUNCHER_RELATIVE, "-m", "apps.analysis.queue_cli", "list")
    real = payload.resolve()
    # The env NAMES are read from the modules that consume them, so a rename on
    # either side turns this red instead of silently shipping a dead export.
    assert own_beatgrid.RUNNER_PYTHON_ENV == "MDT_BEATGRID_RUNNER_PYTHON"
    assert weights.WEIGHTS_PATH_ENV == "MDT_BEATGRID_WEIGHTS"
    assert report["runner_python"] == str(real / payload_beatgrid.RUNNER_LAUNCHER_RELATIVE)
    assert report["weights"] == str(real / payload_beatgrid.CHECKPOINT_RELATIVE)
    assert payload_beatgrid.CHECKPOINT_RELATIVE.name == weights.CHECKPOINT_FILENAME
    assert report["pythonpath"] == f"{real / 'app'}:{real / 'pylib'}"
    assert report["argv"] == "-m apps.analysis.queue_cli list"


def test_engine_launcher_carries_the_same_beatgrid_exports(payload: Path) -> None:
    engine_launcher = (payload / "bin/opendj-engine").read_text(encoding="utf-8")
    python_launcher = (payload / PYTHON_LAUNCHER_RELATIVE).read_text(encoding="utf-8")
    for line in (
        'MDT_BEATGRID_RUNNER_PYTHON="$payload/'
        f'{payload_beatgrid.RUNNER_LAUNCHER_RELATIVE}"',
        f'MDT_BEATGRID_WEIGHTS="$payload/{payload_beatgrid.CHECKPOINT_RELATIVE}"',
        "export MDT_BEATGRID_RUNNER_PYTHON MDT_BEATGRID_WEIGHTS",
    ):
        assert line in engine_launcher
        assert line in python_launcher


def test_python_launcher_is_the_engine_launcher_with_only_the_exec_line_swapped(
    payload: Path,
) -> None:
    """The acceptance harness grafts this launcher onto an older payload for its
    negative control, so it must carry nothing the engine launcher does not."""
    engine = (payload / "bin/opendj-engine").read_text(encoding="utf-8").splitlines()
    python = (payload / PYTHON_LAUNCHER_RELATIVE).read_text(encoding="utf-8").splitlines()
    engine_body = [line for line in engine if not line.startswith("#")]
    python_body = [line for line in python if not line.startswith("#")]
    assert engine_body[:-1] == python_body[:-1]
    assert python_body[-1] == 'exec "$payload/runtime/bin/python3" "$@"'


def test_runner_launcher_isolates_the_runner_site_from_the_engine_closure(
    payload: Path,
) -> None:
    engine_env = {
        "PATH": "/usr/bin:/bin",
        # What the queue worker hands its children: the ENGINE closure.
        "PYTHONPATH": f"{payload / 'app'}:{payload / 'pylib'}",
    }
    report = _run(
        payload / payload_beatgrid.RUNNER_LAUNCHER_RELATIVE,
        "runner.py",
        "--audio",
        "x.mp3",
        env=engine_env,
    )
    assert report["pythonpath"] == str(payload.resolve() / payload_beatgrid.RUNNER_SITE_RELATIVE)
    assert report["argv"] == "runner.py --audio x.mp3"


# ----- runner closure --------------------------------------------------------


def test_runner_pins_are_read_from_the_runner_script_itself() -> None:
    pins = payload_beatgrid.runner_pins(REPO_ROOT / payload_beatgrid.RUNNER_SCRIPT_RELATIVE)
    # Presence of the inference stack the lane's reproducibility rests on.
    assert {"beat-this", "torch", "torchaudio", "numpy", "soundfile"} <= set(pins)
    assert all(version and "," not in version for version in pins.values())


def test_runner_export_carries_every_pep723_pin_at_that_version() -> None:
    pins = payload_beatgrid.runner_pins(REPO_ROOT / payload_beatgrid.RUNNER_SCRIPT_RELATIVE)
    exported = payload_beatgrid.runner_locked_export(REPO_ROOT)
    darwin_lines = [
        line.split(";")[0].strip()
        for line in exported.splitlines()
        if "==" in line and "sys_platform == 'linux'" not in line
    ]
    for name, version in pins.items():
        assert f"{name}=={version}" in darwin_lines, (name, version, darwin_lines)


def test_installed_check_accepts_a_name_locked_at_several_versions_under_disjoint_markers(
    tmp_path: Path,
) -> None:
    """The runner export is universal: networkx differs by Python version and
    torch carries ``+cpu`` on Linux. Either locked version must satisfy the
    check, and a version NO line pins must still fail it."""
    site = tmp_path / "site"
    (site / "networkx-3.6.1.dist-info").mkdir(parents=True)
    locked = parse_locked_export(
        "networkx==3.6.1 ; python_full_version < '3.12'\n"
        "networkx==3.7 ; python_full_version >= '3.12'\n"
    )
    assert_installed_is_locked(site, locked)
    (site / "networkx-3.6.1.dist-info").rename(site / "networkx-3.5.dist-info")
    with pytest.raises(PayloadBuildError, match=r"networkx==3\.5"):
        assert_installed_is_locked(site, locked)


# ----- checkpoint ------------------------------------------------------------


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_fetch_checkpoint_downloads_verifies_and_caches(tmp_path: Path) -> None:
    source = tmp_path / "upstream.ckpt"
    source.write_bytes(b"real checkpoint bytes")
    cache = tmp_path / "cache"
    fetched = payload_beatgrid.fetch_checkpoint(
        cache, url=source.as_uri(), expected_sha256=_sha(b"real checkpoint bytes")
    )
    assert fetched.read_bytes() == b"real checkpoint bytes"
    assert fetched.parent == cache
    # Second call is served from the cache: the source is gone.
    source.unlink()
    again = payload_beatgrid.fetch_checkpoint(
        cache, url=source.as_uri(), expected_sha256=_sha(b"real checkpoint bytes")
    )
    assert again == fetched


def test_fetch_checkpoint_refuses_a_download_with_the_wrong_digest(tmp_path: Path) -> None:
    source = tmp_path / "upstream.ckpt"
    source.write_bytes(b"some other checkpoint")
    cache = tmp_path / "cache"
    with pytest.raises(payload_beatgrid.PayloadBeatgridError, match="sha256"):
        payload_beatgrid.fetch_checkpoint(
            cache, url=source.as_uri(), expected_sha256=_sha(b"the pinned one")
        )
    # Nothing cached: a wrong file must not be found by the next build.
    assert not cache.exists() or not any(cache.iterdir())


def test_fetch_checkpoint_refuses_a_corrupt_cache_entry(tmp_path: Path) -> None:
    expected = _sha(b"the pinned one")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"{expected}.ckpt").write_bytes(b"truncated")
    with pytest.raises(payload_beatgrid.PayloadBeatgridError, match=r"truncated|sha256"):
        payload_beatgrid.fetch_checkpoint(
            cache, url=(tmp_path / "never-read").as_uri(), expected_sha256=expected
        )


def test_stage_checkpoint_copies_byte_identical_to_the_launcher_path(tmp_path: Path) -> None:
    cached = tmp_path / "c.ckpt"
    cached.write_bytes(b"weights")
    payload = tmp_path / "payload"
    staged = payload_beatgrid.stage_checkpoint(payload, cached)
    assert staged == payload / payload_beatgrid.CHECKPOINT_RELATIVE
    assert staged.read_bytes() == b"weights"


# ----- verification -----------------------------------------------------------


def test_verify_refuses_a_payload_without_the_checkpoint(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    (payload / payload_beatgrid.RUNNER_SITE_RELATIVE).mkdir(parents=True)
    with pytest.raises(payload_beatgrid.PayloadBeatgridError, match="checkpoint"):
        payload_beatgrid.verify_bundled_beatgrid_runner(payload, REPO_ROOT)


def test_verify_refuses_a_checkpoint_with_the_wrong_digest(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    (payload / payload_beatgrid.RUNNER_SITE_RELATIVE).mkdir(parents=True)
    staged = payload / payload_beatgrid.CHECKPOINT_RELATIVE
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"not final0")
    with pytest.raises(payload_beatgrid.PayloadBeatgridError, match="sha256"):
        payload_beatgrid.verify_bundled_beatgrid_runner(payload, REPO_ROOT)


def test_verify_refuses_a_payload_without_the_runner_site(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    with pytest.raises(payload_beatgrid.PayloadBeatgridError, match="runner site"):
        payload_beatgrid.verify_bundled_beatgrid_runner(payload, REPO_ROOT)


def test_runner_launcher_is_executable(payload: Path) -> None:
    assert os.access(payload / payload_beatgrid.RUNNER_LAUNCHER_RELATIVE, os.X_OK)
    assert os.access(payload / PYTHON_LAUNCHER_RELATIVE, os.X_OK)
