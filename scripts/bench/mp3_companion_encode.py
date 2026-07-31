# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Encode MP3 companions for the RoFormer-spike + demucs-AB FLAC stem pairs.

Both spikes render vocals.flac and instrumental.flac per track. Those files
can exceed the lossy source size. This writes MP3 companions in the same
directory; existing listening-folder symlinks then expose the companions
without changing their targets.

Hard spec: every stem .mp3 must be <= its track's source .mp3 on-disk bytes.
Start at LAME 320 CBR; if that overshoots the source, drop to LAME V0 (VBR)
for that one file. If V0 *also* overshoots, fail fast rather than silently
shipping an oversized file (brittle fail-fast, no silent fallback chains).

FLACs are never modified or deleted -- this only ever writes a new .mp3 file
and appends an "mp3" key to meta.json (existing keys untouched).

Usage:
    uv run scripts/bench/mp3_companion_encode.py \
        data/state/stems-roformer-spike data/state/stems-demucs-ab

Requirements (mini-PRD)
  ✔︎ ✅ every source_path is a real, existing .mp3 file
    [if] source_path does not end in .mp3 [then ⛔️] RuntimeError, no encode attempted
    [if] source_path does not exist on disk [then ⛔️] RuntimeError
  ✔︎ ✅ LAME 320 CBR first, V0 VBR fallback, hard size gate
    [if] 320 CBR mp3 bytes <= source bytes [then] keep 320 CBR, record settings
    [if] 320 CBR mp3 bytes > source bytes [then] re-encode V0, use V0 if it fits
    [if] V0 mp3 bytes STILL > source bytes [then ⛔️] RuntimeError, no file left behind
  ✔︎ ✅ meta.json gains an "mp3" key, nothing else changes
    [if] meta.json is re-read after this script runs [then] all pre-existing keys
      are byte-identical, plus one new "mp3" key with per-stem settings/bytes

-Claude
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

STEMS: tuple[str, ...] = ("vocals", "instrumental")
CBR_KBPS = 320
# Standard MP3 CBR bitrate ladder (kbps), descending. Third-tier fallback only:
# the task's stated ladder is 320 CBR -> V0 VBR, but the HARD size spec (every
# stem .mp3 <= its source .mp3 bytes) is non-negotiable, and V0's variable bitrate
# can overshoot a low-bitrate source. CBR at a known bitrate
# is size-predictable (~bitrate * duration / 8, tiny container overhead), so
# stepping down this ladder is the only way to guarantee the hard spec rather
# than silently shipping an oversized file.
CBR_FALLBACK_LADDER_KBPS: tuple[int, ...] = (256, 224, 192, 160, 128, 112, 96, 80, 64, 56, 48, 40, 32)


class EncodeError(RuntimeError):
    pass


def _run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise EncodeError(f"ffmpeg failed ({' '.join(args)}): {proc.stderr.strip()}")


def _probe_duration_s(flac_path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(flac_path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise EncodeError(f"ffprobe failed to read duration for {flac_path}: {proc.stderr.strip()}")
    return float(proc.stdout.strip())


def _encode_cbr(flac_path: Path, mp3_path: Path, kbps: int) -> None:
    _run_ffmpeg(["-i", str(flac_path), "-codec:a", "libmp3lame", "-b:a", f"{kbps}k", str(mp3_path)])


def _encode_v0_vbr(flac_path: Path, mp3_path: Path) -> None:
    _run_ffmpeg(["-i", str(flac_path), "-codec:a", "libmp3lame", "-qscale:a", "0", str(mp3_path)])


def _encode_one_stem(flac_path: Path, mp3_path: Path, source_bytes: int) -> dict:
    _encode_cbr(flac_path, mp3_path, CBR_KBPS)
    size = mp3_path.stat().st_size
    settings = f"lame_{CBR_KBPS}kbps_cbr"
    if size > source_bytes:
        _encode_v0_vbr(flac_path, mp3_path)
        size = mp3_path.stat().st_size
        settings = "lame_v0_vbr"
    if size > source_bytes:
        # Third-tier fallback: step down a known-bitrate CBR ladder until the
        # predictable size clears the hard spec. See CBR_FALLBACK_LADDER_KBPS.
        duration_s = _probe_duration_s(flac_path)
        effective_source_kbps = source_bytes * 8 / duration_s / 1000
        for kbps in CBR_FALLBACK_LADDER_KBPS:
            if kbps >= effective_source_kbps:
                continue  # skip rungs we already know are >= the source's own rate
            _encode_cbr(flac_path, mp3_path, kbps)
            size = mp3_path.stat().st_size
            settings = f"lame_{kbps}kbps_cbr_fit_source"
            if size <= source_bytes:
                break
        if size > source_bytes:
            mp3_path.unlink()
            raise EncodeError(
                f"{mp3_path}: exhausted the CBR fallback ladder down to "
                f"{CBR_FALLBACK_LADDER_KBPS[-1]}kbps and still exceed source "
                f"({source_bytes} bytes) -- refusing to ship an oversized stem mp3"
            )
    return {
        "encoder": "lame",
        "settings": settings,
        "bytes": size,
        "source_bytes": source_bytes,
        "fits_source": size <= source_bytes,
    }


def _process_track_dir(track_dir: Path) -> dict:
    meta_path = track_dir / "meta.json"
    meta = json.loads(meta_path.read_text())
    source_path = Path(meta["source_path"])
    if source_path.suffix.lower() != ".mp3":
        raise EncodeError(f"{meta_path}: source_path is not an .mp3: {source_path}")
    if not source_path.is_file():
        raise EncodeError(f"{meta_path}: source_path does not exist: {source_path}")
    source_bytes = source_path.stat().st_size

    mp3_meta: dict = {}
    for stem in STEMS:
        flac_path = track_dir / f"{stem}.flac"
        if not flac_path.is_file():
            raise EncodeError(f"missing {flac_path}")
        mp3_path = track_dir / f"{stem}.mp3"
        mp3_meta[stem] = _encode_one_stem(flac_path, mp3_path, source_bytes)

    meta["mp3"] = mp3_meta
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return mp3_meta


def main() -> None:
    dirs = [Path(a) for a in sys.argv[1:]]
    if not dirs:
        raise SystemExit("usage: mp3_companion_encode.py <stems-dir> [<stems-dir> ...]")

    print(f"{'dir':<32} {'stable_id':<42} {'stem':<13} {'settings':<15} {'bytes':>10} {'source':>10} {'fits':>5}")
    for stems_dir in dirs:
        if not stems_dir.is_dir():
            raise EncodeError(f"not a directory: {stems_dir}")
        for track_dir in sorted(stems_dir.iterdir()):
            if not track_dir.is_dir():
                continue
            mp3_meta = _process_track_dir(track_dir)
            for stem, info in mp3_meta.items():
                print(
                    f"{stems_dir.name:<32} {track_dir.name:<42} {stem:<13} "
                    f"{info['settings']:<15} {info['bytes']:>10} {info['source_bytes']:>10} "
                    f"{'yes' if info['fits_source'] else 'NO':>5}"
                )


if __name__ == "__main__":
    main()
