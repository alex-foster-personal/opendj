"""Offline BPM / semitone track render CLI (issue #12231).

``python -m apps.analysis.render_cli`` decodes a source track, renders a copy
through the Rust Signalsmith stretch path via ``odj-audio track-render``, and
prints a JSON receipt. The source file is never modified.

Exit codes: 0 ok, 2 usage / refused path, 3 engine unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared.odj_audio_binary import OdjAudioUnavailable, find_binary
from apps.shared.odj_audio_decode import decode_to_wav

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RenderReceipt:
    engine: str
    varispeed_fallback: bool
    requested_semitones: float
    measured_semitones: float | None
    measured_duration_s: float
    source_duration_s: float
    output_frames: int
    sample_rate: int
    tempo_ratio: float
    master_tempo: bool
    key_shift_semitones: int

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> RenderReceipt:
        return cls(
            engine=str(raw["engine"]),
            varispeed_fallback=bool(raw["varispeed_fallback"]),
            requested_semitones=float(raw["requested_semitones"]),
            measured_semitones=(None if raw.get("measured_semitones") is None else float(raw["measured_semitones"])),
            measured_duration_s=float(raw["measured_duration_s"]),
            source_duration_s=float(raw["source_duration_s"]),
            output_frames=int(raw["output_frames"]),
            sample_rate=int(raw["sample_rate"]),
            tempo_ratio=float(raw["tempo_ratio"]),
            master_tempo=bool(raw["master_tempo"]),
            key_shift_semitones=int(raw["key_shift_semitones"]),
        )


def _same_path(a: Path, b: Path) -> bool:
    if a == b:
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        ar = a.resolve(strict=False)
        br = b.resolve(strict=False)
        return ar == br


def _refuse_destination(source: Path, dest: Path) -> str | None:
    if _same_path(source.resolve(strict=False), dest.resolve(strict=False)):
        return "destination is the same file as the source"
    if dest.exists():
        return f"destination already exists: {dest}"
    return None


def _write_tone_fixture_wav(
    path: Path,
    *,
    sample_rate: int = 48_000,
    hz: float = 440.0,
    seconds: float = 2.0,
    amplitude: float = 0.5,
) -> None:
    """Synthetic stereo float WAV for tests (small, deterministic)."""
    import math

    frames = int(sample_rate * seconds)
    pcm: list[float] = []
    for i in range(frames):
        v = amplitude * math.sin(2.0 * math.pi * hz * i / sample_rate)
        pcm.extend((v, v))
    payload = b"".join(struct.pack("<f", x) for x in pcm)
    data_len = len(payload)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_len,
        b"WAVE",
        b"fmt ",
        16,
        3,
        2,
        sample_rate,
        sample_rate * 8,
        8,
        32,
        b"data",
        data_len,
    )
    path.write_bytes(header + payload)


def _odj_audio() -> Path:
    return find_binary(os.environ, REPO_ROOT).path


def _track_render(
    engine: Path,
    decoded_wav: Path,
    dest: Path,
    *,
    tempo_ratio: float,
    key_shift: int,
    key_lock: bool,
    source_hz: float | None,
) -> RenderReceipt:
    cmd = [
        str(engine),
        "track-render",
        "--in",
        str(decoded_wav),
        "--out",
        str(dest),
        "--tempo-ratio",
        str(tempo_ratio),
        "--key-shift",
        str(key_shift),
    ]
    if key_lock:
        cmd.append("--key-lock")
    else:
        cmd.append("--master-tempo")
    if source_hz is not None:
        cmd.extend(["--source-hz", str(source_hz)])
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=600.0,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        if "unavailable" in msg:
            raise OdjAudioUnavailable(msg or "stretch engine unavailable")
        raise RuntimeError(msg or f"track-render exited {proc.returncode}")
    line = proc.stdout.strip().splitlines()[-1]
    return RenderReceipt.from_json(json.loads(line))


def render_track(
    source: Path,
    dest: Path,
    *,
    source_bpm: float | None,
    target_bpm: float | None,
    key_lock: bool,
    semitones: int | None,
    source_hz: float | None = None,
) -> RenderReceipt:
    source = source.resolve()
    dest = dest.resolve()
    reason = _refuse_destination(source, dest)
    if reason:
        raise ValueError(reason)
    before = source.read_bytes() if source.is_file() else b""
    if semitones is None:
        if source_bpm is None or target_bpm is None:
            raise ValueError("provide --semitones or both --source-bpm and --target-bpm")
        if source_bpm <= 0 or target_bpm <= 0:
            raise ValueError("BPM values must be positive")
        tempo_ratio = target_bpm / source_bpm
        key_shift = 0
    else:
        tempo_ratio = 1.0
        key_shift = semitones
    if key_lock and semitones is None and abs(tempo_ratio - 1.0) <= 1e-12:
        raise ValueError("key lock with identical BPM needs no render")
    try:
        engine = _odj_audio()
    except OdjAudioUnavailable as exc:
        raise OdjAudioUnavailable(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix="mdt-render-") as tmp:
        info = decode_to_wav(source, Path(tmp), binary=engine)
        decoded = info.path
        hz = source_hz
        receipt = _track_render(
            engine,
            decoded,
            dest,
            tempo_ratio=tempo_ratio,
            key_shift=key_shift,
            key_lock=key_lock or semitones is not None,
            source_hz=hz,
        )
    after = source.read_bytes() if source.is_file() else b""
    if after != before:
        raise RuntimeError("source bytes changed during render")
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.analysis.render_cli",
        description="Render a track copy offline at a target BPM or semitone shift.",
    )
    parser.add_argument("source", type=Path, help="input audio file")
    parser.add_argument("dest", type=Path, help="output path (must not exist)")
    parser.add_argument("--source-bpm", type=float, help="stored BPM of the source")
    parser.add_argument("--target-bpm", type=float, help="target BPM with key lock")
    parser.add_argument(
        "--key-lock",
        action="store_true",
        help="preserve key (Master Tempo) for BPM renders",
    )
    parser.add_argument("--semitones", type=int, help="integer semitone shift (-12..12)")
    parser.add_argument(
        "--source-hz",
        type=float,
        help="fundamental Hz of a synthetic tone fixture for measured shift",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.semitones is not None and (args.source_bpm or args.target_bpm):
        print("use --semitones or BPM flags, not both", file=sys.stderr)
        return EXIT_USAGE
    if (args.source_bpm or args.target_bpm) and not (args.source_bpm and args.target_bpm):
        print("--source-bpm and --target-bpm must be given together", file=sys.stderr)
        return EXIT_USAGE
    if args.target_bpm and not args.key_lock:
        print("target BPM renders require --key-lock", file=sys.stderr)
        return EXIT_USAGE
    try:
        receipt = render_track(
            args.source,
            args.dest,
            source_bpm=args.source_bpm,
            target_bpm=args.target_bpm,
            key_lock=bool(args.key_lock),
            semitones=args.semitones,
            source_hz=args.source_hz,
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    except OdjAudioUnavailable as exc:
        print(exc, file=sys.stderr)
        return EXIT_UNAVAILABLE
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return EXIT_UNAVAILABLE
    print(json.dumps(receipt.__dict__, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
