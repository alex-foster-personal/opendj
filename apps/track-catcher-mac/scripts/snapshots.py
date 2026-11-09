#!/usr/bin/env python3
"""Snapshot every Track Catcher state and prove each one rendered.

    python3 apps/track-catcher-mac/scripts/snapshots.py OUT_DIR

Runs the built app with `--snapshot OUT_DIR`, then checks for the presence
of each good thing rather than the absence of errors: every expected PNG
exists and is not a single flat color, the engine's readout of the demo is
in the log, and the CLI's `wheel` and `analyze` print what the design says.
Exits 1 naming each missing piece. Build first with scripts/bundle.py.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
# PNG header fields (IHDR): 8-bit depth, colour types RGB and RGBA.
PNG_DEPTH = 8
PNG_RGB = 2
PNG_RGBA = 6
RGB_BYTES = 3
RGBA_BYTES = 4
# A real window shows more than this many colours, a flat fill does not.
MIN_DISTINCT_COLORS = 4
ARGC = 2  # script name plus the output directory
APP = HERE / "dist" / "Open DJ Track Catcher.app" / "Contents" / "MacOS"
STATES = [
    "window",
    "settings",
    "capture-menu",
    "key-scores",
    "piano-ring",
    "toast",
    "setup-1",
    "setup-2",
    "first-launch",
    "no-mic",
    "silent",
    "drop-progress",
    "compact",
    "now-playing",
    "drop-result",
]


class UnreadablePng(ValueError):
    """A PNG this probe cannot inspect: never counted as varied."""


def distinct_colors(png: Path, limit: int = 16) -> int:
    """Count distinct pixel values in an 8-bit RGB/RGBA PNG, up to `limit`.

    Any other bit depth or color type raises UnreadablePng, so a flat
    grayscale, indexed or 16-bit snapshot can't pass by going unexamined.
    """
    data = png.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return 0
    pos, idat, width, height, ctype = 8, b"", 0, 0, 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", body[:10])
            if depth != PNG_DEPTH or ctype not in (PNG_RGB, PNG_RGBA):
                raise UnreadablePng(f"bit depth {depth}, color type {ctype}; only 8-bit RGB/RGBA is read")
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    bpp = RGBA_BYTES if ctype == PNG_RGBA else RGB_BYTES
    stride = width * bpp + 1
    seen: set[bytes] = set()
    # Sample rows without unfiltering: identical filtered rows of a flat image
    # stay identical, and any real drawing makes them differ.
    for y in range(0, height, max(1, height // 64)):
        row = raw[y * stride : (y + 1) * stride]
        seen.add(row)
        if len(seen) >= limit:
            break
    return len(seen)


def log_problems(log: str) -> list[str]:
    """What the app's own snapshot log says went wrong.

    Now playing (Apple Events) and the drop zone (the app's async analysis)
    must each log a finished result: a hang or a failure is UNAVAILABLE and
    fails the gate rather than shipping a "Reading..." or progress-bar PNG.
    """
    problems: list[str] = []
    if "FAILED" in log:
        problems.append("snapshot log reports FAILED")
    if "engine: demo fed 40 s; shown 12B" not in log:
        problems.append("the engine's demo readout (12B) is not in the log")
    lines = log.splitlines()
    for step in ("now-playing", "drop-progress", "drop"):
        found = [line for line in lines if line.startswith(f"{step}: ")]
        if not found:
            problems.append(f"the snapshot log has no {step} result")
        elif "UNAVAILABLE" in found[0]:
            problems.append(found[0])
    return problems


def clear_outputs(out: Path) -> None:
    """Remove what an earlier run left, so only this run's files get checked."""
    names = ["snapshots.txt", "demo-96s.wav", "cli-wheel.txt", "cli-analyze.txt", "snapshot-inbox.jsonl"]
    names += [f"{n}{suffix}.png" for n in STATES for suffix in ("", "-screen")]
    for name in names:
        (out / name).unlink(missing_ok=True)


def main() -> int:
    if len(sys.argv) != ARGC:
        sys.exit(__doc__)
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    clear_outputs(out)
    problems: list[str] = []
    # The output folder reaches the children as their working directory, not as
    # an argument: a path built from argv is user data to the subprocess rule.
    run = subprocess.run(
        [str(APP / "TrackCatcher"), "--snapshot", "."],
        cwd=out,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    print(run.stdout)
    print(run.stderr, file=sys.stderr)
    if run.returncode != 0:
        problems.append(f"TrackCatcher --snapshot exited {run.returncode}")
    log = (out / "snapshots.txt").read_text(encoding="utf-8") if (out / "snapshots.txt").exists() else ""
    problems += log_problems(log)
    for name in STATES:
        png = out / f"{name}.png"
        if not png.exists():
            problems.append(f"{name}.png missing")
        else:
            try:
                if distinct_colors(png) < MIN_DISTINCT_COLORS:
                    problems.append(f"{name}.png is flat (nothing rendered)")
            except UnreadablePng as e:
                problems.append(f"{name}.png UNKNOWN: can't inspect it ({e})")
    # Only the variables the CLI needs, not the whole runner environment: the
    # CLI reads ODJ_LIVE to find the engine, and PATH and HOME for its own tools.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if k in os.environ}
    env["ODJ_LIVE"] = str(APP / "odj-live")
    wheel = subprocess.run(
        [str(APP / "track-catcher"), "wheel", "12B"], capture_output=True, text=True, env=env, check=False
    )
    (out / "cli-wheel.txt").write_text(wheel.stdout, encoding="utf-8")
    if "[12B]/12A" not in wheel.stdout:
        problems.append("track-catcher wheel 12B did not bracket 12B")
    analyze = subprocess.run(
        [str(APP / "track-catcher"), "analyze", "demo-96s.wav"],
        cwd=out,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    (out / "cli-analyze.txt").write_text(analyze.stdout + analyze.stderr, encoding="utf-8")
    if not analyze.stdout.startswith("12B · E major") or "key change at" not in analyze.stdout:
        problems.append(f"track-catcher analyze printed {analyze.stdout.strip() or analyze.stderr.strip()!r}")
    if problems:
        print("snapshots: FAILED\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    print(f"snapshots: {len(STATES)} states rendered, CLI checks passed, in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
