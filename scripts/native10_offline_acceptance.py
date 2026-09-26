"""NATIVE-10 acceptance: an installed payload runs a v1 backfill with networking denied.

``python -m scripts.native10_offline_acceptance --payload DIR --library DIR
--work DIR --ffmpeg PATH``

What it does, in order, and what each step rules out:

1. INSTALL: copies the built payload with ``ditto`` into a fresh
   ``Open DJ.app/Contents/Resources/payload`` under ``--work``, so a path
   baked in at build time cannot answer for the installed copy.
2. CLEAN ROOM: every payload process gets ``PATH=/usr/bin:/bin`` (no ``uv``,
   no Homebrew), an EMPTY ``HOME`` (no Torch Hub cache, no uv cache, no
   weights in a data dir) and a fresh ``MDT_DATA_DIR``. ffmpeg is the one
   host prerequisite, passed explicitly through ``MDT_FFMPEG`` because the
   payload does not ship a decoder (ADR-NEW-packaged-vocal-runtime).
3. NO NETWORK: every payload process runs under ``sandbox-exec`` with
   ``(deny network*)``. The instrument is proven before it is trusted: the
   same socket connect must FAIL inside the sandbox and SUCCEED outside it,
   or the run reports UNKNOWN and exits 3 rather than a verdict.
4. BACKFILL: folder-ingests ``--library`` into a new state.db, then for each
   lane in beatgrid, key, waveform, loudness (key reads beatgrid's own
   downbeats, so order matters) enqueues every track and drains the batch
   through ``bin/opendj-python -m apps.analysis.queue_cli``.
5. VERDICT, BY PRESENCE: a lane passes only when its drain exits 0, EVERY
   track has an ``own_<lane>.backfill`` record, and at least one of those
   records is ``ok`` carrying a real measurement (beats, a key with
   bar-synchronous segments over beatgrid's own downbeats, tri-band peaks,
   a finite LUFS). A track that is not ``ok`` passes only with a named
   PRODUCER outcome from ``PRODUCER_OUTCOMES`` (the analysis ran and judged
   the music, e.g. a key with no tonal center): those are what the same
   producer returns on the same track from a networked checkout, so they
   are the lane working, not the install failing. Any other reason, or a
   missing record, fails. A clean exit code with no records is a failure.

A payload older than ``bin/opendj-python`` gets that ONE file grafted from
its own engine launcher (the engine launcher with its exec line swapped,
which tests/scripts/test_engine_payload_beatgrid.py pins as the shipped
file's exact shape). Nothing else about the payload under test changes, so
the negative control fails for the payload's own reasons, not the harness's.

Exit codes: 0 every lane passed, 1 at least one lane failed, 3 UNKNOWN (the
network denial could not be proven, or an input is missing).

-Claude
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LANES: tuple[str, ...] = ("beatgrid", "key", "waveform", "loudness")
SANDBOX_PROFILE = "(version 1)\n(allow default)\n(deny network*)\n"
PYTHON_LAUNCHER = Path("bin/opendj-python")
ENGINE_LAUNCHER = Path("bin/opendj-engine")
PROBE_HOST = ("1.1.1.1", 443)
EXIT_PASS, EXIT_FAIL, EXIT_UNKNOWN = 0, 1, 3
STEP_TIMEOUT_S = 3600
#: Per lane, the reason prefixes that mean "the producer ran and judged this
#: track", never "the install could not run it". Observed reasons only: a new
#: one fails until someone reads it and adds it here on purpose.
PRODUCER_OUTCOMES: dict[str, tuple[str, ...]] = {
    "beatgrid": ("bar_phase_below_floor",),
    "key": ("no_tonal_center",),
    "waveform": (),
    "loudness": (),
}


@dataclass
class LaneVerdict:
    lane: str
    drain_exit: int | None = None
    tracks_total: int = 0
    tracks_ok: int = 0
    tracks_producer_outcome: int = 0
    seconds: float = 0.0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        judged = self.tracks_ok + self.tracks_producer_outcome
        return (self.drain_exit == 0 and self.tracks_ok >= 1
                and judged == self.tracks_total and not self.failures)


class Unmeasurable(RuntimeError):
    """The harness cannot establish its own preconditions; no verdict exists."""


#-----------------------------------------------------------------------------
# install + clean room
#-----------------------------------------------------------------------------

def install_payload(payload: Path, work: Path) -> tuple[Path, bool]:
    """``(installed payload dir, whether opendj-python had to be grafted)``."""
    installed = work / "Open DJ.app" / "Contents" / "Resources" / "payload"
    installed.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/usr/bin/ditto", str(payload), str(installed)], check=True)
    launcher = installed / PYTHON_LAUNCHER
    if launcher.is_file():
        return installed, False
    engine = (installed / ENGINE_LAUNCHER).read_text(encoding="utf-8").rstrip("\n").splitlines()
    engine[-1] = 'exec "$payload/runtime/bin/python3" "$@"'
    launcher.write_text("\n".join(engine) + "\n", encoding="utf-8")
    launcher.chmod(0o755)
    return installed, True


def clean_env(work: Path, ffmpeg: Path) -> dict[str, str]:
    home, data, tmp = work / "home", work / "data", work / "tmp"
    for path in (home, data, tmp):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "MDT_DATA_DIR": str(data),
        "MDT_FFMPEG": str(ffmpeg),
        "OPENDJ_TELEMETRY": "0",
        "AF_SERVICE_ID": "com.af.opendj.native10-acceptance",
    }


def sandboxed(argv: list[str]) -> list[str]:
    return ["/usr/bin/sandbox-exec", "-p", SANDBOX_PROFILE, *argv]


def prove_network_denied(installed: Path, env: dict[str, str]) -> dict[str, str]:
    """Positive AND negative control for the sandbox, with the payload's own python."""
    probe = (
        "import socket,sys\n"
        f"try:\n socket.create_connection({PROBE_HOST!r}, 5).close()\n"
        "except OSError as e:\n print(type(e).__name__, e); sys.exit(7)\n"
        "print('connected')\n"
    )
    python = str(installed / "runtime/bin/python3")
    inside = subprocess.run(sandboxed([python, "-c", probe]), env=env,
                            capture_output=True, text=True, check=False)
    try:
        socket.create_connection(PROBE_HOST, 5).close()
        outside = "connected"
    except OSError as exc:
        raise Unmeasurable(
            f"this host cannot reach {PROBE_HOST} WITHOUT the sandbox ({exc}), so "
            "a failed connect inside it would prove nothing"
        ) from exc
    if inside.returncode != 7 or "Operation not permitted" not in inside.stdout:
        raise Unmeasurable(f"sandbox did not deny the network: {inside.stdout} {inside.stderr}")
    return {"inside_sandbox": inside.stdout.strip(), "outside_sandbox": outside}


#-----------------------------------------------------------------------------
# backfill
#-----------------------------------------------------------------------------

def run_payload(
    installed: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        sandboxed([str(installed / PYTHON_LAUNCHER), *args]),
        env=env, cwd=env["TMPDIR"], capture_output=True, text=True,
        check=False, timeout=STEP_TIMEOUT_S,
    )


def ingest_library(installed: Path, env: dict[str, str], db: Path, library: Path) -> list[str]:
    for args in (("init",), ("ingest-folder", "--root", str(library), "--write")):
        done = run_payload(installed, env, "-m", "apps.shared.state.cli", "--db", str(db), *args)
        if done.returncode != 0:
            raise Unmeasurable(f"state.cli {args[0]} failed:\n{done.stdout}\n{done.stderr}")
    with sqlite3.connect(db) as conn:
        ids = [row[0] for row in conn.execute(
            "SELECT stable_id FROM tracks WHERE deleted_at IS NULL ORDER BY stable_id")]
    if not ids:
        raise Unmeasurable(f"folder ingest of {library} produced no tracks")
    return ids


def drain_lane(installed: Path, env: dict[str, str], db: Path, lane: str,
               stable_ids: list[str]) -> LaneVerdict:
    verdict = LaneVerdict(lane=lane, tracks_total=len(stable_ids))
    backend = f"own_{lane}.backfill"
    queue = ("-m", "apps.analysis.queue_cli", "--db", str(db), "--json")
    ids = [arg for sid in stable_ids for arg in ("--stable-id", sid)]
    started = time.monotonic()
    enqueued = run_payload(installed, env, *queue, "enqueue", "--lane", lane,
                           "--backend", backend, *ids)
    if enqueued.returncode != 0:
        verdict.failures.append(f"enqueue exit {enqueued.returncode}: {enqueued.stderr[-1500:]}")
        return verdict
    batch_id = json.loads(enqueued.stdout)["batch_id"]
    drained = run_payload(
        installed, env, *queue, "run", "--batch-id", batch_id, "--backend", backend
    )
    verdict.drain_exit = drained.returncode
    verdict.seconds = round(time.monotonic() - started, 1)
    if drained.returncode != 0:
        verdict.failures.append(f"drain exit {drained.returncode}: "
                                f"{(drained.stderr or drained.stdout)[-2500:]}")
    return verdict


#-----------------------------------------------------------------------------
# verdict by presence
#-----------------------------------------------------------------------------

def _measurement(lane: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """The lane's real measurement, or None when the block carries no value."""
    if lane == "beatgrid":
        beats, bpm = payload.get("beats") or [], payload.get("bpm")
        ok = len(beats) >= 32 and isinstance(bpm, int | float) and 40 <= bpm <= 250
        return {"beats": len(beats), "bpm": bpm} if ok else None
    if lane == "key":
        # The v1 key lane segments over beatgrid's OWN downbeats, so a key
        # without bar-synchronous segments is the lane running half-installed.
        camelot = payload.get("camelot")
        segments = payload.get("segments") or {}
        ok = bool(camelot) and segments.get("status") == "ok" and segments.get("segments")
        measured = {"camelot": camelot, "openkey": payload.get("openkey"),
                    "segments": len(segments.get("segments") or [])}
        return measured if ok else None
    if lane == "waveform":
        detail = payload.get("detail") or {}
        length = detail.get("length") or 0
        peak = max(max(detail.get(b) or [0]) for b in ("low", "mid", "high")) if length else 0
        return {"detail_length": length, "max_band": peak} if length > 0 and peak > 0 else None
    if lane == "loudness":
        lufs = payload.get("integrated_lufs")
        ok = isinstance(lufs, int | float) and math.isfinite(lufs) and -60 < lufs < 0
        measured = {"integrated_lufs": lufs, "true_peak_dbtp": payload.get("true_peak_dbtp")}
        return measured if ok else None
    raise ValueError(f"unknown lane {lane!r}")


def judge_lane(db: Path, verdict: LaneVerdict, stable_ids: list[str]) -> LaneVerdict:
    backend = f"own_{verdict.lane}.backfill"
    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute(
            "SELECT stable_id, record_json FROM analysis WHERE backend = ?", (backend,)
        ).fetchall())
    for sid in stable_ids:
        if sid not in rows:
            verdict.failures.append(f"{sid}: no {backend} record")
            continue
        block = (json.loads(rows[sid]).get("lanes") or {}).get(verdict.lane) or {}
        measured = _measurement(verdict.lane, block.get("payload") or {})
        reason = str(block.get("reason") or "")
        # str.startswith(()) is False, so a lane with no outcomes admits none.
        if block.get("status") == "failed" and reason.startswith(PRODUCER_OUTCOMES[verdict.lane]):
            verdict.tracks_producer_outcome += 1
            verdict.evidence.append({"stable_id": sid, "producer_outcome": reason})
            continue
        if block.get("status") != "ok" or measured is None:
            verdict.failures.append(
                f"{sid}: {verdict.lane} status={block.get('status')} reason={reason}")
            continue
        verdict.tracks_ok += 1
        verdict.evidence.append({"stable_id": sid, **measured})
    return verdict


#-----------------------------------------------------------------------------
# main
#-----------------------------------------------------------------------------

def run_acceptance(payload: Path, library: Path, work: Path, ffmpeg: Path) -> dict[str, Any]:
    for required in (payload / ENGINE_LAUNCHER, library, ffmpeg):
        if not required.exists():
            raise Unmeasurable(f"missing input {required}")
    installed, grafted = install_payload(payload, work)
    env = clean_env(work, ffmpeg)
    network = prove_network_denied(installed, env)
    db = work / "data" / "state" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    stable_ids = ingest_library(installed, env, db, library)
    lanes = [judge_lane(db, drain_lane(installed, env, db, lane, stable_ids), stable_ids)
             for lane in LANES]
    manifest = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
    return {
        "payload": str(payload),
        "payload_git_sha": manifest["identity"]["git_sha_full"],
        "payload_bytes_total": manifest["bytes"]["total"],
        "grafted_python_launcher": grafted,
        "network": network,
        "tracks": len(stable_ids),
        "passed": all(v.passed for v in lanes),
        "lanes": [{**asdict(v), "passed": v.passed} for v in lanes],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--report", type=Path, help="also write the JSON report here")
    args = parser.parse_args(argv)
    if args.work.exists() and any(args.work.iterdir()):
        print(f"[ERROR] --work {args.work} must be empty: a reused data dir is not a fresh install",
              file=sys.stderr)
        return EXIT_UNKNOWN
    try:
        report = run_acceptance(args.payload.resolve(), args.library.resolve(),
                                args.work.resolve(), args.ffmpeg)
    except Unmeasurable as exc:
        print(f"[UNKNOWN] {exc}", file=sys.stderr)
        return EXIT_UNKNOWN
    text = json.dumps(report, indent=2)
    print(text)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    for lane in report["lanes"]:
        mark = "[OK]" if lane["passed"] else "[FAIL]"
        print(f"{mark} {lane['lane']}: {lane['tracks_ok']}/{lane['tracks_total']} ok with a "
              f"measurement, {lane['tracks_producer_outcome']} named producer outcome, "
              f"drain exit {lane['drain_exit']} ({lane['seconds']}s)", file=sys.stderr)
    return EXIT_PASS if report["passed"] else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
