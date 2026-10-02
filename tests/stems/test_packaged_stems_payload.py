"""Stems run from a STAGED payload, with no uv, no modal and no checkout.

[if] a stems route cannot run from the staged payload [then] fail, [else stop].

Issue #3421: every stems producer was a ``scripts/`` file the payload did not
ship, two routes also needed a ``uv`` the app does not ship, and the relay
path imported modal. Every existing stems test ran from a repo checkout,
where ``scripts/`` exists by construction, so none of them could see it.

These tests build the payload's ``app/`` tree with the real
``stage_app_source``, point ``PROJECT_ROOT`` at it, set the interpreter the
payload launcher exports, and then:

  - [if] any argv a stems route can emit names a file that is not in the
    STAGED tree [then] fail [stop if a path resolves only inside the checkout]
  - [if] any of those argvs starts with uv or asks for a modal overlay
    [then] fail [stop if the app would need a binary it does not ship]
  - [if] the relay worker, spawned from the staged tree with ``import modal``
    made to fail, cannot write a bundle [then] fail

Mutation check, run by hand on this branch: dropping
``_stage_stems_workers`` from ``stage_app_source`` turns the argv test red
on its first route; reverting the relay worker to the farm's bundle writer
turns the subprocess test red on ``import modal``.

-Claude
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from apps.stems import job as stems_job
from apps.stems import worker_launch
from scripts.build_engine_payload import stage_app_source

REPO_ROOT = Path(__file__).resolve().parents[2]
SID = "c" * 40

pytestmark = pytest.mark.requirement("STEM-39")


@pytest.fixture(scope="module")
def staged_app(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``payload/app`` exactly as the payload build stages it."""
    root = tmp_path_factory.mktemp("payload")
    build_dir = root / "frontend-build"
    build_dir.mkdir()
    (build_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    destination = root / "payload" / "app"
    stage_app_source(REPO_ROOT, destination, build_dir)
    return destination


@pytest.fixture
def packaged(monkeypatch: pytest.MonkeyPatch, staged_app: Path) -> Path:
    """This process now resolves workers the way the installed engine does."""
    monkeypatch.setattr("apps.shared.platform_paths.PROJECT_ROOT", staged_app)
    monkeypatch.setenv("MDT_STEM_WORKER_PYTHON", sys.executable)
    monkeypatch.delenv("MDT_STEMS_TRANSPORT", raising=False)
    # A uv that does not exist: a route that still reaches for uv must fail
    # here rather than pass on this machine's own uv.
    monkeypatch.setenv("MDT_UV_BIN", "uv-is-not-shipped-in-the-app")
    return staged_app


def _assert_runs_from_payload(argv: list[str], staged: Path) -> None:
    assert argv[0] == sys.executable, f"not the packaged interpreter: {argv}"
    assert "uv" not in Path(argv[0]).name
    assert "--with" not in argv
    scripts = [arg for arg in argv if arg.endswith(".py")]
    assert scripts, f"no worker script in {argv}"
    for arg in scripts:
        path = Path(arg)
        assert path.is_absolute(), f"relative worker path {arg}"
        assert path.is_relative_to(staged), f"{arg} resolves outside the payload"
        assert path.is_file(), f"{arg} is not in the staged payload"


def test_every_payload_worker_script_is_staged(staged_app: Path) -> None:
    """[if] a worker the spawn side names is missing from the payload [then] fail."""
    for relative in worker_launch.PAYLOAD_WORKER_SCRIPTS:
        staged = staged_app / relative
        assert staged.is_file(), f"{relative} not staged"
        assert staged.read_bytes() == (REPO_ROOT / relative).read_bytes()


def test_negative_control_an_unstaged_script_reports_missing(packaged: Path) -> None:
    """The probe can say no: a name the payload does not carry reads missing."""
    refusal = worker_launch.missing_worker("scripts/zzz_not_a_real_worker.py")
    assert refusal is not None and str(packaged) in refusal


@pytest.mark.parametrize("executor", ["local", "modal"])
def test_compute_routes_run_from_the_payload(
    packaged: Path, monkeypatch: pytest.MonkeyPatch, executor: str
) -> None:
    """[if] the local or relay route names uv or a checkout path [then] fail."""
    monkeypatch.setattr("apps.stems.routing.resolve_stems_executor", lambda **_: executor)
    monkeypatch.setattr("apps.stems.local_gate.local_stems_gate", lambda: None)
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source", lambda _d: None
    )
    argv = stems_job.build_argv({"stable_ids": [SID], "tier": "M"})
    _assert_runs_from_payload(argv, packaged)


def test_r2_first_route_runs_from_the_payload(
    packaged: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the R2-first route names a checkout path [then] fail."""
    monkeypatch.setattr("apps.stems.routing.resolve_stems_executor", lambda **_: "modal")
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source", lambda _d: object()
    )
    argv = stems_job.build_argv({"stable_ids": [SID], "tier": "M"})
    assert argv[1].endswith("stems_r2_first_worker.py")
    _assert_runs_from_payload(argv, packaged)


def test_cloud_hydrate_route_runs_from_the_payload(packaged: Path, tmp_path: Path) -> None:
    """[if] cloud.hydrate names a checkout path [then] fail (same class as #3421)."""
    from apps.cloud import job as cloud_job

    argv = cloud_job.build_argv(
        {
            "stable_id": SID,
            "asset_kind": "stem_bundle",
            "machine_id": "machine-1",
            "data_dir": str(tmp_path),
        }
    )
    _assert_runs_from_payload(argv, packaged)


def test_the_app_refuses_the_direct_transport(
    packaged: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the app accepted a direct job [then] it would die on import modal."""
    monkeypatch.setattr("apps.stems.routing.resolve_stems_executor", lambda **_: "modal")
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source", lambda _d: None
    )
    monkeypatch.setenv("MDT_STEMS_TRANSPORT", "direct")
    with pytest.raises(stems_job.StemsJobPayloadError, match="modal"):
        stems_job.build_argv({"stable_ids": [SID], "tier": "M"})


# ----- a real relay run from the staged tree ---------------------------------


def _packaged_env(staged: Path, extra: Path) -> dict[str, str]:
    """The launcher's environment contract, minus the checkout.

    ``extra`` carries a ``modal`` package that raises on import, so a worker
    that still reaches for modal fails here even on a machine (CI) that has
    modal installed, and the separator double, copied out of ``tests/`` so
    the checkout is not on the path at all.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONPATH"] = os.pathsep.join([str(extra), str(staged)])
    env["PYTHONSAFEPATH"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MDT_STEM_WORKER_PYTHON"] = sys.executable
    env["MDT_UV_BIN"] = "uv-is-not-shipped-in-the-app"
    env["MDT_STEMS_SEPARATOR"] = "stems_separator_double:separate"
    env.pop("MDT_STEMS_TRANSPORT", None)
    return env


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="the separator double encodes real audio with ffmpeg and ffprobe",
)
def test_relay_worker_writes_a_bundle_from_the_payload_without_modal(
    packaged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the relay worker cannot run from the payload without modal [then] fail."""
    extra = tmp_path / "extra"
    (extra / "modal").mkdir(parents=True)
    (extra / "modal" / "__init__.py").write_text(
        'raise ImportError("modal is not shipped in the installed app")\n',
        encoding="utf-8",
    )
    shutil.copy2(
        REPO_ROOT / "tests" / "stems" / "modal_separator_double.py",
        extra / "stems_separator_double.py",
    )
    env = _packaged_env(packaged, extra)

    # Control: under this env, apps and scripts resolve from the STAGED tree,
    # and modal really cannot be imported. Without it, a pass could come from
    # the checkout's editable install.
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import apps, scripts, json\n"
            "try:\n import modal\n m = 'importable'\n"
            "except ImportError:\n m = 'blocked'\n"
            "print(json.dumps([apps.__path__[0], scripts.__path__[0], m]))",
        ],
        env=env,
        cwd="/",
        capture_output=True,
        text=True,
        check=True,
    )
    apps_dir, scripts_dir, modal_state = json.loads(probe.stdout)
    assert Path(apps_dir).is_relative_to(packaged)
    assert Path(scripts_dir).is_relative_to(packaged)
    assert modal_state == "blocked"

    data_dir = tmp_path / "data"
    audio = tmp_path / "music" / "c.mp3"
    audio.parent.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=44100",
            "-ac", "2", "-b:a", "192k", str(audio),
        ],
        check=True,
    )
    state_db = data_dir / "state" / "state.db"
    state_db.parent.mkdir(parents=True)
    connection = sqlite3.connect(state_db)
    try:
        connection.execute(
            "CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, file_path TEXT, "
            "duration_ms INTEGER)"
        )
        connection.execute("INSERT INTO tracks VALUES (?, ?, ?)", (SID, str(audio), 3000))
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr("apps.stems.routing.resolve_stems_executor", lambda **_: "modal")
    monkeypatch.setattr(
        "apps.cloud.stem_source.resolve_stem_hydration_source", lambda _d: None
    )
    argv = stems_job.build_argv(
        {"stable_ids": [SID], "tier": "M", "data_dir": str(data_dir)}
    )
    _assert_runs_from_payload(argv, packaged)

    result = subprocess.run(
        argv, env=env, cwd="/", capture_output=True, text=True, timeout=300, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(line.get(stems_job.PROGRESS_TRACK_KEY) == SID for line in lines)

    bundle = data_dir / "state" / "stems" / SID
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stable_id"] == SID
    assert manifest["model"]["name"].startswith("test-double:")
    for name in manifest["files"].values():
        assert (bundle / name).read_bytes()[:4] == b"fLaC"
