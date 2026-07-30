#!/usr/bin/env python3
"""Spin up a disposable Linux GCE GPU box for demucs vocal farm-out.

Why this exists: bifrost2 (Windows gaming PC) was the wrong shape for a
farm worker (cmd shell, GPU-idle gate, NTFS quoting, Steve's session).
A preemptible Linux GCE VM with CUDA is the iterate-fast path: create,
ship a batch, run ``scripts/vocal_worker_runner.py``, pull JSON, delete.

Prereqs (one-time, human):
  1. ``gcloud`` installed + logged in (already true on this Mac).
  2. Enable Compute Engine API on the project:
     https://console.cloud.google.com/apis/library/compute.googleapis.com
  3. Request GPU quota if needed (NVIDIA_T4_GPUS >= 1 in the zone).
  4. Optional billing budget alert -- T4 spot is cheap but not free.

Env (required / overridable; see .env.sample):
  MDT_VOCAL_GCE_PROJECT   REQUIRED -- your GCP project id (no shared default)
  MDT_VOCAL_GCE_PROFILE   l4 (default) | t4 | cpu
                          cpu = no GPU (many accounts never get GPU quota)
  MDT_VOCAL_GCE_ZONE      default us-east1-c (l4) / us-central1-a (cpu|t4)
  MDT_VOCAL_GCE_INSTANCE  default mdt-vocals
  MDT_VOCAL_GCE_MACHINE   profile default (g2-standard-8 / n1-standard-4 / n2-standard-8)
  MDT_VOCAL_GCE_GPU       ignored when profile=cpu
  MDT_VOCAL_GCE_DISK_GB   default 100
  MDT_VOCAL_GCE_PREEMPT   default 1 (spot/preemptible)

Commands:
  python scripts/vocal_gcloud_farm.py status
  python scripts/vocal_gcloud_farm.py up
  python scripts/vocal_gcloud_farm.py bootstrap     # uv + ffmpeg + cuda torch venv
  python scripts/vocal_gcloud_farm.py ship --batch DIR
  python scripts/vocal_gcloud_farm.py run           # once over inbox
  python scripts/vocal_gcloud_farm.py pull --dest DIR
  python scripts/vocal_gcloud_farm.py import-outbox --batch DIR --outbox DIR
  python scripts/vocal_gcloud_farm.py down
  python scripts/vocal_gcloud_farm.py stage-playlist --playlist NAME [--limit N]

Typical loop:
  stage-playlist -> up -> bootstrap -> ship -> run -> pull -> import-outbox -> down

-Claude
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared.paths import DATA_DIR
from apps.vocals import cache as vcache
from apps.vocals.cli import (
    CATEGORY_TODO,
    WORKER_SCRIPT,
    Ctx,
    best_playlist_rank,
    classify,
    load_tracks,
    order_todo,
)

# ----- CFG -------------------------------------------------------------------
# Project id is NEVER defaulted: each operator sets MDT_VOCAL_GCE_PROJECT in
# .env and/or Doppler (construct/dev_af, or personal general/dev_personal).
#
# Profiles: many personal GCP accounts never get GPUS_ALL_REGIONS > 1 (or
# stay at 0). ``cpu`` is the portable default path -- no accelerator quota.
PROFILES: dict[str, dict[str, str]] = {
    "l4": {
        "zone": "us-east1-c",
        "machine": "g2-standard-8",
        "gpu": "nvidia-l4",
        "device": "cuda",
        "image_project": "deeplearning-platform-release",
        "image_family": "common-cu129-ubuntu-2204-nvidia-580",
    },
    "t4": {
        "zone": "us-central1-a",
        "machine": "n1-standard-4",
        "gpu": "nvidia-tesla-t4",
        "device": "cuda",
        "image_project": "deeplearning-platform-release",
        "image_family": "common-cu129-ubuntu-2204-nvidia-580",
    },
    "cpu": {
        "zone": "us-central1-a",
        "machine": "n2-standard-8",
        "gpu": "",
        "device": "cpu",
        "image_project": "ubuntu-os-cloud",
        "image_family": "ubuntu-2204-lts",
    },
}
DEFAULT_PROFILE = "l4"
DEFAULT_INSTANCE = "mdt-vocals"
DEFAULT_DISK_GB = "100"
REMOTE_ROOT = Path("/opt/mdt-vocals")
REMOTE_REPO = REMOTE_ROOT / "repo"
REMOTE_WORK = REMOTE_ROOT / "work"
REMOTE_VENV_PY = REMOTE_ROOT / "venv" / "bin" / "python"


@dataclass(frozen=True)
class GceCfg:
    project: str
    zone: str
    instance: str
    machine: str
    gpu: str
    disk_gb: str
    preempt: bool
    profile: str
    device: str
    image_project: str
    image_family: str

    @property
    def ssh_target(self) -> str:
        return f"{self.instance}.{self.zone}.{self.project}"

    @property
    def is_cpu(self) -> bool:
        return self.profile == "cpu" or self.device == "cpu"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        raise SystemExit(f"error: {name} is empty")
    return value


def load_cfg() -> GceCfg:
    project = os.environ.get("MDT_VOCAL_GCE_PROJECT", "").strip()
    if not project:
        raise SystemExit(
            "error: MDT_VOCAL_GCE_PROJECT is required (your GCP project id).\n"
            "  Set it in .env, or: doppler secrets set MDT_VOCAL_GCE_PROJECT=...\n"
            "  Then: doppler run -- python scripts/vocal_gcloud_farm.py ..."
        )
    profile = os.environ.get("MDT_VOCAL_GCE_PROFILE", DEFAULT_PROFILE).strip().lower()
    if profile not in PROFILES:
        raise SystemExit(
            f"error: MDT_VOCAL_GCE_PROFILE={profile!r} unknown; "
            f"use one of {sorted(PROFILES)}"
        )
    base = PROFILES[profile]
    preempt = _env("MDT_VOCAL_GCE_PREEMPT", "1") not in ("0", "false", "False")
    # Profile table is source of truth for machine/gpu/image. Stale MACHINE=g2-*
    # in .env must not win when PROFILE=cpu. Set MDT_VOCAL_GCE_FORCE_SHAPE=1 to
    # honor MACHINE/GPU/IMAGE_* env overrides. ZONE always overridable (stockout).
    force_shape = os.environ.get("MDT_VOCAL_GCE_FORCE_SHAPE", "").strip() in (
        "1",
        "true",
        "True",
    )
    machine = base["machine"]
    gpu = base["gpu"]
    image_project = base["image_project"]
    image_family = base["image_family"]
    if force_shape:
        machine = (
            os.environ.get("MDT_VOCAL_GCE_MACHINE", machine).strip() or machine
        )
        if base["device"] != "cpu":
            gpu = os.environ.get("MDT_VOCAL_GCE_GPU", gpu).strip()
        image_project = (
            os.environ.get("MDT_VOCAL_GCE_IMAGE_PROJECT", image_project).strip()
            or image_project
        )
        image_family = (
            os.environ.get("MDT_VOCAL_GCE_IMAGE_FAMILY", image_family).strip()
            or image_family
        )
    zone = (
        os.environ.get("MDT_VOCAL_GCE_ZONE", base["zone"]).strip() or base["zone"]
    )
    return GceCfg(
        project=project,
        zone=zone,
        instance=_env("MDT_VOCAL_GCE_INSTANCE", DEFAULT_INSTANCE),
        machine=machine,
        gpu=gpu,
        disk_gb=_env("MDT_VOCAL_GCE_DISK_GB", DEFAULT_DISK_GB),
        preempt=preempt,
        profile=profile,
        device=base["device"],
        image_project=image_project,
        image_family=image_family,
    )


# ----- gcloud helpers --------------------------------------------------------


def _gcloud(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["gcloud", *args]
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.stdout:
        print(proc.stdout.rstrip(), flush=True)
    if proc.returncode != 0:
        if proc.stderr:
            print(proc.stderr.rstrip(), file=sys.stderr, flush=True)
        if check:
            raise SystemExit(f"error: gcloud exited {proc.returncode}")
    return proc


def _gcloud_out(args: list[str]) -> str:
    proc = _gcloud(args, check=True)
    return (proc.stdout or "").strip()


def _gcloud_ok(args: list[str]) -> bool:
    return _gcloud(args, check=False).returncode == 0


def instance_exists(cfg: GceCfg) -> bool:
    proc = _gcloud(
        [
            "compute",
            "instances",
            "describe",
            cfg.instance,
            f"--project={cfg.project}",
            f"--zone={cfg.zone}",
        ],
        check=False,
    )
    err = (proc.stderr or "") + (proc.stdout or "")
    if "SERVICE_DISABLED" in err or "has not been used" in err:
        raise SystemExit(
            "error: Compute Engine API is disabled on this project.\n"
            f"  Enable: https://console.developers.google.com/apis/api/"
            f"compute.googleapis.com/overview?project={cfg.project}\n"
            "  Then request NVIDIA_T4_GPUS quota in the zone if needed."
        )
    return proc.returncode == 0


def ssh(
    cfg: GceCfg, remote_cmd: str, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return _gcloud(
        [
            "compute",
            "ssh",
            cfg.instance,
            f"--project={cfg.project}",
            f"--zone={cfg.zone}",
            "--command",
            remote_cmd,
        ],
        check=check,
    )


def scp_to(cfg: GceCfg, local: Path, remote: str) -> None:
    _gcloud(
        [
            "compute",
            "scp",
            "--recurse",
            f"--project={cfg.project}",
            f"--zone={cfg.zone}",
            str(local),
            f"{cfg.instance}:{remote}",
        ],
        check=True,
    )


def scp_from(cfg: GceCfg, remote: str, local: Path) -> None:
    local.mkdir(parents=True, exist_ok=True)
    _gcloud(
        [
            "compute",
            "scp",
            "--recurse",
            f"--project={cfg.project}",
            f"--zone={cfg.zone}",
            f"{cfg.instance}:{remote}",
            str(local),
        ],
        check=True,
    )


# ----- commands --------------------------------------------------------------


def cmd_status(cfg: GceCfg) -> int:
    print(f"project={cfg.project} zone={cfg.zone} instance={cfg.instance}")
    print(
        f"profile={cfg.profile} machine={cfg.machine} gpu={cfg.gpu or '-'} "
        f"device={cfg.device} preempt={cfg.preempt}"
    )
    if not instance_exists(cfg):
        print("instance: ABSENT")
        return 0
    print("instance: PRESENT")
    if cfg.is_cpu:
        ssh(cfg, "nproc; free -h | head -2")
    else:
        ssh(
            cfg,
            "nvidia-smi --query-gpu=name,utilization.gpu,memory.used "
            "--format=csv,noheader",
        )
    return 0


def cmd_up(cfg: GceCfg) -> int:
    if instance_exists(cfg):
        print(f"instance {cfg.instance} already exists")
        return 0
    args = [
        "compute",
        "instances",
        "create",
        cfg.instance,
        f"--project={cfg.project}",
        f"--zone={cfg.zone}",
        f"--machine-type={cfg.machine}",
        f"--boot-disk-size={cfg.disk_gb}GB",
        "--boot-disk-type=pd-balanced",
        f"--image-family={cfg.image_family}",
        f"--image-project={cfg.image_project}",
        "--scopes=cloud-platform",
    ]
    if not cfg.is_cpu:
        if not cfg.gpu:
            raise SystemExit("error: GPU profile requires MDT_VOCAL_GCE_GPU")
        args += [
            f"--accelerator=count=1,type={cfg.gpu}",
            "--maintenance-policy=TERMINATE",
            "--metadata=install-nvidia-driver=True",
        ]
    if cfg.preempt:
        args += ["--provisioning-model=SPOT", "--instance-termination-action=STOP"]
    _gcloud(args, check=True)
    print("waiting for SSH...")
    for _ in range(30):
        proc = ssh(cfg, "echo ready", check=False)
        if proc.returncode == 0:
            print(proc.stdout.strip() or "ready")
            return 0
        time.sleep(10)
    raise SystemExit("error: instance created but SSH never became ready")


def cmd_bootstrap(cfg: GceCfg) -> int:
    """Install uv + ffmpeg + CUDA torch venv; sync the thin repo slice."""
    repo = Path(__file__).resolve().parents[1]
    # Ship only what the runner needs (no data/, no frontend).
    slice_dir = Path("/tmp/mdt-vocals-slice")
    if slice_dir.exists():
        shutil.rmtree(slice_dir)
    for rel in (
        "scripts/vocal_region_worker.py",
        "scripts/vocal_worker_runner.py",
        "apps/__init__.py",
        "apps/vocals",
        "apps/shared/__init__.py",
        "apps/shared/paths.py",
        "apps/shared/platform_paths.py",
        "pyproject.toml",
    ):
        src = repo / rel
        dst = slice_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)

    ssh(cfg, f"sudo mkdir -p {REMOTE_ROOT} && sudo chown -R $USER:$USER {REMOTE_ROOT}")
    scp_to(cfg, slice_dir, str(REMOTE_REPO))

    if cfg.is_cpu:
        torch_lines = """
uv pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu
python -c 'import torch; print("torch", torch.__version__, "cuda", torch.cuda.is_available())'
"""
    else:
        # CUDA torch from the pytorch wheel index -- plain PyPI is CPU-only.
        torch_lines = """
uv pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu124
python -c 'import torch; assert torch.cuda.is_available(), "cuda missing"; print("cuda", torch.version.cuda, torch.cuda.get_device_name(0))'
"""
    remote = f"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! command -v ffmpeg >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y ffmpeg
fi
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
cd {REMOTE_REPO}
uv venv {REMOTE_ROOT}/venv --python 3.12
# shellcheck disable=SC1091
source {REMOTE_ROOT}/venv/bin/activate
uv pip install demucs==4.0.1 'numpy<2' soundfile
{torch_lines}
mkdir -p {REMOTE_WORK}/inbox {REMOTE_WORK}/outbox {REMOTE_WORK}/logs
echo bootstrap_ok
"""
    ssh(cfg, remote)
    return 0


def cmd_ship(cfg: GceCfg, batch: Path) -> int:
    if not batch.is_dir():
        raise SystemExit(f"error: batch dir missing: {batch}")
    files = [p for p in batch.iterdir() if p.is_file()]
    if not files:
        raise SystemExit(f"error: batch dir empty: {batch}")
    ssh(
        cfg,
        f"rm -rf {REMOTE_WORK}/inbox/* {REMOTE_WORK}/outbox/*; mkdir -p {REMOTE_WORK}/inbox {REMOTE_WORK}/outbox {REMOTE_WORK}/logs",
    )
    scp_to(cfg, batch, f"{REMOTE_WORK}/inbox-staging")
    # flatten staging/* into inbox (scp --recurse creates a named dir)
    ssh(
        cfg,
        f"mv {REMOTE_WORK}/inbox-staging/* {REMOTE_WORK}/inbox/ && rmdir {REMOTE_WORK}/inbox-staging && ls {REMOTE_WORK}/inbox | wc -l",
    )
    return 0


def cmd_run(cfg: GceCfg) -> int:
    device = cfg.device
    remote = f"""
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
export MDT_VOCAL_WORKER_PYTHON={REMOTE_VENV_PY}
export MDT_VOCAL_WORKER_DEVICE={device}
export PYTHONPATH={REMOTE_REPO}
cd {REMOTE_REPO}
{REMOTE_VENV_PY} scripts/vocal_worker_runner.py \\
  --inbox {REMOTE_WORK}/inbox \\
  --outbox {REMOTE_WORK}/outbox \\
  --logs {REMOTE_WORK}/logs \\
  --device {device} --once
"""
    ssh(cfg, remote)
    return 0


def cmd_pull(cfg: GceCfg, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    # scp the directory; flatten into dest/
    staging = dest / "_pull_staging"
    if staging.exists():
        shutil.rmtree(staging)
    scp_from(cfg, f"{REMOTE_WORK}/outbox", staging)
    nested = staging / "outbox"
    src = nested if nested.is_dir() else staging
    n = 0
    for path in src.glob("*.json"):
        shutil.copy2(path, dest / path.name)
        n += 1
    shutil.rmtree(staging, ignore_errors=True)
    print(f"pulled -> {dest} ({n} json)")
    return 0


def cmd_down(cfg: GceCfg) -> int:
    if not instance_exists(cfg):
        print("instance already absent")
        return 0
    _gcloud(
        [
            "compute",
            "instances",
            "delete",
            cfg.instance,
            f"--project={cfg.project}",
            f"--zone={cfg.zone}",
            "--quiet",
        ],
        check=True,
    )
    return 0


# ----- Mac-side stage + import ----------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_stage_playlist(
    playlist: str,
    dest: Path,
    *,
    limit: int,
    data_dir: Path,
) -> int:
    """Build a farm batch from todos: <stable_id>.<ext> + manifest.jsonl."""
    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, playlist)
    classify(ctx, tracks)
    rank = best_playlist_rank(ctx, tracks)
    todo = order_todo([t for t in tracks if t.category == CATEGORY_TODO], rank)
    batch = todo[:limit]
    if not batch:
        print("no todo tracks to stage")
        return 0
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    worker_sha = _sha256_file(WORKER_SCRIPT)
    manifest_path = dest / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as mf:
        for tr in batch:
            assert tr.audio_path is not None
            ext = tr.audio_path.suffix.lower() or ".bin"
            shipped = f"{tr.stable_id}{ext}"
            target = dest / shipped
            # Prefer WAV for sphn; if already wav/mp3/flac just copy.
            # m4a stays as-is if ffmpeg decode works on the DLVM (it does).
            shutil.copy2(tr.audio_path, target)
            st = tr.audio_path.stat()
            row = {
                "stable_id": tr.stable_id,
                "title": tr.title,
                "src_path": str(tr.audio_path),
                "shipped_name": shipped,
                "audio_mtime": st.st_mtime,
                "size_bytes": st.st_size,
                "worker_sha256": worker_sha,
            }
            mf.write(json.dumps(row) + "\n")
            print(f"staged {shipped} ({tr.title!r})")
    print(f"staged {len(batch)} tracks -> {dest}")
    print(f"manifest -> {manifest_path}")
    return 0


def _unwrap_runner_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split farm meta from demucs worker fields.

    ``vocal_worker_runner`` writes ``{**demucs_result, worker: {script_sha256,
    host, device_used}}``. Demucs fields stay at top level; only the
    ``worker`` key is farm meta (demucs itself has no top-level worker key).
    """
    meta = payload.get("worker") if isinstance(payload.get("worker"), dict) else {}
    if "script_sha256" in meta:
        worker_result = {k: v for k, v in payload.items() if k != "worker"}
        return worker_result, meta
    return payload, {}


def cmd_import_outbox(batch: Path, outbox: Path, data_dir: Path) -> int:
    """Import pulled result JSONs using the HANDOFF mtime + worker-hash gates."""
    manifest: dict[str, dict[str, Any]] = {}
    man_path = batch / "manifest.jsonl"
    if not man_path.is_file():
        raise SystemExit(f"error: missing manifest: {man_path}")
    for line in man_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        manifest[row["stable_id"]] = row

    imported = 0
    skipped = 0
    for path in sorted(outbox.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        worker_result, meta = _unwrap_runner_payload(payload)
        stable_id = path.stem
        row = manifest.get(stable_id)
        if row is None:
            print(f"[SKIP] {stable_id}: not in manifest")
            skipped += 1
            continue
        got_sha = meta.get("script_sha256")
        expect_sha = row.get("worker_sha256")
        if got_sha and expect_sha and got_sha != expect_sha:
            print(f"[SKIP] {stable_id}: worker sha mismatch")
            skipped += 1
            continue
        src = row.get("src_path") or row.get("file_path")
        if not src:
            print(f"[SKIP] {stable_id}: manifest missing src_path/file_path")
            skipped += 1
            continue
        audio = Path(src)
        if not audio.is_file():
            print(f"[SKIP] {stable_id}: audio missing locally")
            skipped += 1
            continue
        staged_mtime = row.get("audio_mtime")
        if staged_mtime is not None and audio.stat().st_mtime != staged_mtime:
            print(f"[SKIP] {stable_id}: audio mtime drifted since stage")
            skipped += 1
            continue
        entry = vcache.write_entry(
            vcache.cache_path(data_dir, stable_id),
            worker_result,
            audio,
            audio_mtime=float(staged_mtime) if staged_mtime is not None else audio.stat().st_mtime,
        )
        print(
            f"[OK] {stable_id} regions={len(entry['regions'])} "
            f"cov={entry['coverage_pct']}%"
        )
        imported += 1
    print(f"import done: {imported} ok, {skipped} skipped")
    return 0 if imported > 0 or skipped == 0 else 1


# ----- CLI -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python scripts/vocal_gcloud_farm.py",
        description="Disposable Linux GCE GPU farm for demucs vocals",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show cfg + instance + nvidia-smi")
    sub.add_parser("up", help="create spot GPU instance")
    sub.add_parser("bootstrap", help="install uv/ffmpeg/cuda-torch + ship code")
    ship = sub.add_parser("ship", help="upload a staged batch dir to inbox")
    ship.add_argument("--batch", type=Path, required=True)
    sub.add_parser("run", help="process inbox once on the VM")
    pull = sub.add_parser("pull", help="download outbox JSON to local dest")
    pull.add_argument("--dest", type=Path, required=True)
    sub.add_parser("down", help="delete the instance")

    stage = sub.add_parser("stage-playlist", help="stage todo tracks for a playlist")
    stage.add_argument("--playlist", required=True)
    stage.add_argument("--dest", type=Path, required=True)
    stage.add_argument("--limit", type=int, default=50)
    stage.add_argument("--data-dir", type=Path, default=DATA_DIR)

    imp = sub.add_parser("import-outbox", help="import pulled JSON into vocal-cache")
    imp.add_argument(
        "--batch", type=Path, required=True, help="staged batch with manifest.jsonl"
    )
    imp.add_argument("--outbox", type=Path, required=True)
    imp.add_argument("--data-dir", type=Path, default=DATA_DIR)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Mac-only commands need no live instance.
    if args.cmd == "stage-playlist":
        return cmd_stage_playlist(
            args.playlist,
            args.dest,
            limit=args.limit,
            data_dir=args.data_dir,
        )
    if args.cmd == "import-outbox":
        return cmd_import_outbox(args.batch, args.outbox, args.data_dir)

    cfg = load_cfg()
    if args.cmd == "status":
        return cmd_status(cfg)
    if args.cmd == "up":
        return cmd_up(cfg)
    if args.cmd == "bootstrap":
        return cmd_bootstrap(cfg)
    if args.cmd == "ship":
        return cmd_ship(cfg, args.batch)
    if args.cmd == "run":
        return cmd_run(cfg)
    if args.cmd == "pull":
        return cmd_pull(cfg, args.dest)
    if args.cmd == "down":
        return cmd_down(cfg)
    raise SystemExit(f"unknown cmd {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
