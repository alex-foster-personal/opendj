#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Fetch and decode the 7-track GiantSteps+ modulation-boundary fixture.

WHY ONLY 7 OF 600. `ops/giantsteps-plus/manifest.json`'s `timestamp_verification`
block records the audit: of the 23 GiantSteps+ excerpts annotated with more
than one key (a modulation), 16 carry a start_time far outside the ~120s
excerpt (always a multiple of 60, suggesting a x60 unit-scale bug in that
subset of the original export). This script never guesses a correction --
spec section 5 ("GiantSteps+ once its timestamps are confirmed") requires a
VERIFIED timestamp, not a plausible one, so those 16 stay excluded and only
the 7 with an in-range, sorted, unambiguous boundary are fetched.

WHY A RANGE-READ INSTEAD OF THE FULL 855.8 MB ARCHIVE. The dataset ships as
one `audio.zip` on Zenodo. The server answers HTTP Range requests (confirmed
live: a `bytes=0-1023` request returns 206), so this reads the zip's central
directory and pulls only the 7 needed entries by byte range -- a few MB
total instead of 855.8 MB. `_HTTPRangeFile` is the minimal file-like object
`zipfile.ZipFile` needs (`read`, `seek`, `tell`); it is deliberately stdlib
only, matching `scripts/beatbench/fixtures.py`'s convention of not pulling a
dependency into a fetch script a test's environment did not ask for.

WHY THE MP3 SHA256 IS VERIFIED, NOT JUST THE DOWNLOAD SIZE. A byte count
matching by coincidence is not evidence of the right bytes (a truncated or
substituted download can still land on a plausible size). The digest pinned
in `manifest.json` is compared after every fetch, and a mismatch raises
rather than silently caching bad bytes -- the same discipline
`scripts/beatbench/fixtures.py`'s rebuild path uses for its own excerpts.

AUDIO NEVER ENTERS GIT. `data/*` is gitignored repo-wide; this script writes
into `data/bench/key/giantsteps_plus/` and nothing under `data/` is ever
staged. Only `ops/giantsteps-plus/manifest.json` (annotations + hashes) is
committed.

Usage:
    python scripts/giantsteps_plus_fixture.py fetch
    python scripts/giantsteps_plus_fixture.py fetch --out-dir data/bench/key/giantsteps_plus
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "ops" / "giantsteps-plus" / "manifest.json"
DEFAULT_OUT_DIR = Path("data/bench/key/giantsteps_plus")


class _HTTPRangeFile:
    """The minimal read/seek/tell surface `zipfile.ZipFile` needs, over HTTP.

    Fetches a range at a time with plain `urllib.request`, no dependency
    beyond the standard library. Not general-purpose: it exists to let
    `zipfile` read one remote archive's central directory and a handful of
    member ranges without downloading the whole file.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as resp:
            length = resp.headers.get("Content-Length")
        if length is None:
            raise SystemExit(f"[giantsteps+] {url} did not report Content-Length")
        self._size = int(length)
        self._pos = 0

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._size + offset
        else:
            raise ValueError(f"unsupported whence {whence}")
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, n: int = -1) -> bytes:
        end = self._size - 1 if n is None or n < 0 else min(self._pos + n, self._size) - 1
        if end < self._pos:
            return b""
        req = urllib.request.Request(
            self._url, headers={"Range": f"bytes={self._pos}-{end}"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status not in (200, 206):
                raise SystemExit(
                    f"[giantsteps+] range fetch got HTTP {resp.status} for "
                    f"bytes={self._pos}-{end}"
                )
            data = resp.read()
        self._pos += len(data)
        return data


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def fetch(out_dir: Path) -> int:
    manifest = _load_manifest()
    source = manifest["source"]
    fixtures = manifest["fixtures"]
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    remote = _HTTPRangeFile(source["archive_url"])
    if remote._size != source["archive_content_length"]:
        raise SystemExit(
            f"[giantsteps+] archive Content-Length changed: manifest recorded "
            f"{source['archive_content_length']}, server now reports {remote._size}. "
            "The Zenodo record may have been revised; re-verify before trusting entries."
        )

    with zipfile.ZipFile(remote) as zf:
        for row in fixtures:
            tid = row["id"]
            mp3_path = audio_dir / f"{tid}.mp3"
            wav_path = audio_dir / f"{tid}.wav"
            mp3_replaced = False
            if mp3_path.exists() and _sha256_bytes(mp3_path.read_bytes()) == row["mp3_sha256"]:
                print(f"[giantsteps+] {tid}.mp3 already cached and verified")
            else:
                print(f"[giantsteps+] fetching {row['zip_entry']!r}")
                data = zf.read(row["zip_entry"])
                digest = _sha256_bytes(data)
                if digest != row["mp3_sha256"]:
                    raise SystemExit(
                        f"[giantsteps+] {tid}: fetched bytes hash {digest[:16]}, "
                        f"manifest pins {row['mp3_sha256'][:16]} -- refusing to "
                        "cache a mismatched fixture"
                    )
                mp3_path.write_bytes(data)
                mp3_replaced = True
                print(f"[giantsteps+]   wrote {mp3_path} ({len(data)} bytes, sha256 verified)")

            # A replaced MP3 must invalidate any existing WAV: an
            # `mp3_path.exists()` hash mismatch above (corruption, a bumped
            # manifest digest) means whatever WAV is already on disk was
            # decoded from the OLD bytes, and the measurement test reads only
            # the WAV -- so a stale WAV would silently keep serving the wrong
            # audio as the real test subject even though the MP3 hash just
            # passed (sol-review #3948 P1 BLOCKING).
            if mp3_replaced and wav_path.exists():
                wav_path.unlink()
            if not wav_path.exists():
                proc = subprocess.run(
                    [
                        "ffmpeg", "-y", "-v", "error", "-i", str(mp3_path),
                        "-ac", "1", "-ar", "44100", str(wav_path),
                    ],
                    capture_output=True, text=True, check=False,
                )
                if proc.returncode != 0:
                    raise SystemExit(
                        f"[giantsteps+] {tid}: ffmpeg decode failed: {proc.stderr.strip()[-300:]}"
                    )
                print(f"[giantsteps+]   decoded {wav_path}")

    print(f"[giantsteps+] {len(fixtures)} fixtures ready under {audio_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    fetch_ap = sub.add_parser("fetch", help="download + decode the 7 verified fixtures")
    fetch_ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = ap.parse_args(argv)

    if args.command == "fetch":
        return fetch(args.out_dir)
    raise SystemExit(f"[giantsteps+] unknown command {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
