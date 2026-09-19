#!/usr/bin/env python3
"""Per-hop RMS envelopes, stored beside the vocal cache rather than inside it.

WHY PERSIST THIS. Vocal regions are a PURE FUNCTION of the per-hop vocals_rms
and mix_rms arrays: ``ratio_envelope`` then ``regions_from_envelope``, both
torch-free. Keeping the arrays means every future choice of ON_RATIO,
OFF_RATIO, MERGE_GAP_S, MIN_REGION_S, any absolute level floor, and the
confidence formula becomes a local re-derivation in seconds, instead of a GPU
re-separation of the whole library. It has twice already cost us a measurement
to persist only the derived output and throw the input away.

SCOPE IT HONESTLY: this frees the THRESHOLD family only. It does NOT make
model, overlap, shifts or HOP_S free, because all four change the envelope
itself and so require a real re-separation.

WHY A SIDECAR, NOT A KEY IN THE CACHE ENTRY. ``vocals_for_content`` in
apps/webui/server/rb_vendor.py parses a whole cache entry for EVERY hydrated
row in a listing, and entries run a median of ~1.45 KB. Inlining ~1.8 KB of
envelope would roughly triple the bytes a thousand-row playlist parses, for
data the listing never looks at: it needs regions and coverage only. So the
arrays live in their own file and the hot path is untouched.

WHY v AND m SEPARATELY, NOT THE RATIO. Neither is recoverable from the ratio.
An absolute-floor analysis needs absolute vocals_rms; a low-mix-energy analysis
needs mix_rms. Storing both as float16 is also SMALLER than storing the ratio
alone as 3-decimal JSON.

WHY float16 IS SAFE, verified rather than assumed: across 15,498 real frames
only 11 fell below float16's smallest normal (6.1e-5, itself above the
``m > 1e-6`` silence guard in ratio_envelope), the worst round-trip error on
the derived ratio was 0.00078 against thresholds of 0.10 and 0.05, and NO frame
changed which side of a threshold it sat on. RE-CHECK THAT if HOP_S, ON_RATIO
or OFF_RATIO ever change.

FORMAT: one JSON header line, then raw little-endian float16, vocals then mix.
Self-describing, needs no numpy to write or to validate, and reads back in one
``numpy.frombuffer``. The header carries the same ``audio_signature`` as the
cache entry, so a sidecar that has drifted from its entry is DETECTABLE rather
than silently wrong.

  ✔︎ ✅ 🎯 a written envelope round-trips to the same values within float16.
    [if] read_envelope returns arrays [then] they match what was written
    [if] the file is truncated mid-array [then ⛔️] raise, never pad
  ✔︎ ✅ 🎯 absent means "older entry", never an error.
    [if] no sidecar exists for a stable_id [then] read_envelope returns None
  ✔︎ ✅ 🎯 a sidecar whose signature disagrees with its entry is refused.
    [if] signatures differ [then ⛔️] raise rather than serve stale envelopes

-Claude
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENVELOPE_SCHEMA: int = 1
ENVELOPE_DTYPE: str = "float16"
ENVELOPE_SUFFIX: str = ".env"
ENVELOPE_DIRNAME: str = "envelopes"


@dataclass(frozen=True)
class Envelope:
    """One track's per-hop RMS pair, plus what it was computed from."""

    stable_id: str
    hop_s: float
    frames: int
    audio_signature: dict[str, int]
    vocals_rms: Any  # numpy float32 array, widened from the stored float16
    mix_rms: Any


def envelope_dir(cache_dir: Path) -> Path:
    """Sidecars live under the cache dir so one delete removes both."""
    return cache_dir / ENVELOPE_DIRNAME


def envelope_path(cache_dir: Path, stable_id: str) -> Path:
    return envelope_dir(cache_dir) / f"{stable_id}{ENVELOPE_SUFFIX}"


def write_envelope(
    path: Path,
    stable_id: str,
    hop_s: float,
    frames: int,
    vocals_rms_bytes: bytes,
    mix_rms_bytes: bytes,
    audio_signature: dict[str, int],
) -> Path:
    """Write one sidecar atomically.

    The arrays arrive already float16-encoded, because the container that
    computed them has numpy and the Mac may not need it. Doing the narrowing
    remotely also keeps the return payload small.
    """
    expected = frames * 2  # float16 is 2 bytes
    for name, blob in (("vocals_rms", vocals_rms_bytes), ("mix_rms", mix_rms_bytes)):
        if len(blob) != expected:
            raise ValueError(
                f"{name} for {stable_id} is {len(blob)} bytes, expected "
                f"{expected} for {frames} float16 frames"
            )
    header = {
        "schema": ENVELOPE_SCHEMA,
        "stable_id": stable_id,
        "hop_s": hop_s,
        "frames": frames,
        "dtype": ENVELOPE_DTYPE,
        "order": ["vocals_rms", "mix_rms"],
        "audio_signature": audio_signature,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(
        json.dumps(header, separators=(",", ":")).encode("utf-8")
        + b"\n"
        + vocals_rms_bytes
        + mix_rms_bytes
    )
    tmp.replace(path)
    return path


def read_envelope(path: Path, expect_signature: dict[str, int] | None = None):
    """Read one sidecar, or None if it was never written.

    None means "entry predates envelope persistence", which is the normal state
    for the oldest cache entries and must never read as an error.
    """
    import numpy as np

    if not path.is_file():
        return None
    raw = path.read_bytes()
    split = raw.find(b"\n")
    if split < 0:
        raise ValueError(f"{path}: no header line")
    header = json.loads(raw[:split])
    if header.get("schema") != ENVELOPE_SCHEMA:
        raise ValueError(
            f"{path}: schema {header.get('schema')} != {ENVELOPE_SCHEMA}"
        )
    if header.get("dtype") != ENVELOPE_DTYPE:
        raise ValueError(f"{path}: dtype {header.get('dtype')!r} unsupported")
    frames = int(header["frames"])
    body = raw[split + 1:]
    if len(body) != frames * 2 * 2:
        raise ValueError(
            f"{path}: body is {len(body)} bytes, expected {frames * 4} for "
            f"{frames} frames of two float16 arrays"
        )
    if expect_signature is not None and header["audio_signature"] != expect_signature:
        raise ValueError(
            f"{path}: audio_signature disagrees with the cache entry, so this "
            "envelope describes a different generation of the source file"
        )
    pair = np.frombuffer(body, dtype="<f2").astype("float32")
    return Envelope(
        stable_id=header["stable_id"],
        hop_s=float(header["hop_s"]),
        frames=frames,
        audio_signature=header["audio_signature"],
        vocals_rms=pair[:frames],
        mix_rms=pair[frames:],
    )
