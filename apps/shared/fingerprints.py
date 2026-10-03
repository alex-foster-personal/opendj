"""Chromaprint fingerprint helpers (Phase 7 dedup).

Fingerprints are computed on the device by the Rust engine
(``odj-audio fingerprint``, rusty-chromaprint over symphonia), which the
packaged app ships, so duplicate detection works in a shipped build with
no system install and no network call. Its output is the same string
chromaprint's ``fpcalc`` prints (algorithm 2, first 120 s, compressed and
URL-safe base64), so fingerprints cached from either backend compare.
Where no engine binary resolves (a checkout with no cargo build), the old
pyacoustid + ``fpcalc`` path is the fallback. The AcoustID web service is
never called: it is free for non-commercial use only.

Design notes:

* We cache fingerprints in a SQLite file keyed by
  ``(abs_path, size, mtime)`` so re-scans are O(rows) instead of
  O(rows * ~0.4s-of-decode). The cache also persists duration / bitrate
  because we use them for the cross-bitrate-twin similarity guard.
* ``compute`` raises :class:`ChromaprintMissing` when neither backend is
  available, and :class:`FingerprintFailed` when a backend ran but could
  not fingerprint that one file.
* ``compare`` is a Hamming-distance fraction over the 32-bit
  sub-fingerprints, decoded from the compressed string by
  :func:`decode_fingerprint` (a port of chromaprint's decompressor, so it
  needs no libchromaprint either).

The module is intentionally side-effect-free: no prints, no logging
configuration; callers wire in ``rich`` progress at the CLI layer.
"""
from __future__ import annotations

import base64
import functools
import json
import os
import sqlite3
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from apps.shared import fs_residency

# pyacoustid import is lazy so the module can be imported even when the
# package is missing (useful for test environments that stub it out). The
# real ``compute`` call will raise ``ChromaprintMissing`` if fpcalc is
# missing, so we never silently return bogus data.


class ChromaprintMissing(RuntimeError):
    """Raised when no fingerprint backend is available.

    Neither the engine binary (``odj-audio fingerprint``) nor pyacoustid's
    ``fpcalc`` could be found. pyacoustid surfaces its half as
    ``acoustid.NoBackendError`` or an ``OSError`` depending on version. We
    normalise to a single typed exception so callers can show the
    remediation message.
    """


class FingerprintFailed(RuntimeError):
    """A backend ran but could not fingerprint this one file (corrupt,
    unsupported codec, too short). Other files are unaffected."""


# ------------------------------------------------------------------ types


@dataclass(frozen=True)
class Fingerprint:
    """A computed chromaprint fingerprint + the bits needed to cache it.

    ``fp_str`` is the base64-ish string returned by ``fpcalc`` /
    pyacoustid. ``duration`` is in seconds (float). ``size`` and
    ``mtime`` pin the cache key. ``bitrate`` and ``computed_at`` are
    stored for reporting / debugging but are not part of the identity.
    """

    path: Path
    duration: float
    fp_str: str
    size: int
    mtime: float
    bitrate: int | None = None
    computed_at: str | None = None


# ---------------------------------------------------------------- compute


def _require_acoustid():
    """Import pyacoustid or raise ChromaprintMissing.

    Separated so tests can monkeypatch the import path.
    """
    try:
        import acoustid  # type: ignore
    except Exception as exc:  # pragma: no cover -- pip install gate
        raise ChromaprintMissing(
            "pyacoustid is not installed. Add it to requirements.txt and "
            "run `pip install -r requirements.txt`."
        ) from exc
    return acoustid


# Seconds of audio fingerprinted, as fpcalc does by default.
FINGERPRINT_LENGTH_S = 120
# A whole-file decode of one track is well under this on any machine.
_ENGINE_TIMEOUT_S = 120


_ENGINE_BIN_ENV = "ODJ_AUDIO_BIN"
_ENGINE_EXE = "odj-audio.exe" if os.name == "nt" else "odj-audio"


def _engine_binary() -> Path | None:
    """The odj-audio binary to fingerprint with, or None.

    The same rule as ``apps.engine_core.audio_engine.resolve_binary`` (not
    imported: ``engine_core`` already depends on ``shared``):
    ``ODJ_AUDIO_BIN`` (set by the packaged app) wins and is never
    second-guessed, else the newest release or debug cargo build in this
    checkout.
    """
    from apps.shared.platform_paths import PROJECT_ROOT

    raw = os.environ.get(_ENGINE_BIN_ENV, "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_file() and os.access(p, os.X_OK) else None
    builds = [
        PROJECT_ROOT / "apps" / "audio-engine" / "target" / profile / _ENGINE_EXE
        for profile in ("release", "debug")
    ]
    found = [p for p in builds if p.is_file() and os.access(p, os.X_OK)]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _compute_with_engine(binary: Path, path: Path) -> tuple[float, str]:
    try:
        proc = subprocess.run(
            [str(binary), "fingerprint", str(path), "--length", str(FINGERPRINT_LENGTH_S)],
            capture_output=True,
            text=True,
            timeout=_ENGINE_TIMEOUT_S,
            check=False,
        )
    except OSError as exc:
        raise ChromaprintMissing(f"cannot run {binary}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FingerprintFailed(f"fingerprinting {path} timed out") from exc
    if "unknown command fingerprint" in proc.stderr:
        # An engine build older than the fingerprint command.
        raise ChromaprintMissing(
            f"{binary} has no fingerprint command; rebuild apps/audio-engine"
        )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        raise FingerprintFailed(
            f"odj-audio fingerprint printed nothing for {path} "
            f"(exit {proc.returncode}): {proc.stderr.strip()[-300:]}"
        )
    row = json.loads(lines[-1])
    if "error" in row or proc.returncode != 0:
        raise FingerprintFailed(str(row.get("error") or proc.stderr.strip()[-300:]))
    return float(row["duration"]), str(row["fingerprint"])


def _compute_with_fpcalc(path: Path) -> tuple[float, str]:
    acoustid = _require_acoustid()
    try:
        duration, fp = acoustid.fingerprint_file(str(path))
    except Exception as exc:
        # NoBackendError is the documented path; OSError sometimes leaks
        # through depending on platform. Normalise either way.
        name = type(exc).__name__
        if name in {"NoBackendError", "FingerprintGenerationError"} or isinstance(
            exc, (OSError, FileNotFoundError)
        ):
            raise ChromaprintMissing(
                f"no odj-audio engine build and fpcalc not available: {exc}. "
                "Build apps/audio-engine (cargo build --release) or install "
                "chromaprint."
            ) from exc
        raise
    fp_str = fp.decode("ascii") if isinstance(fp, bytes) else str(fp)
    return float(duration), fp_str


def compute(path: Path) -> Fingerprint:
    """Compute a chromaprint fingerprint for ``path``.

    Raises
    ------
    ChromaprintMissing
        When no fingerprint backend is available.
    FingerprintFailed
        When the engine could not fingerprint this file.
    FileNotFoundError
        When ``path`` does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(str(path))
    binary = _engine_binary()
    if binary is None:
        duration, fp_str = _compute_with_fpcalc(path)
    else:
        try:
            duration, fp_str = _compute_with_engine(binary, path)
        except ChromaprintMissing:
            # An engine build that cannot fingerprint (older than the
            # command, or not runnable) must not hide a working fpcalc:
            # a checkout or CI runner can hold a stale cargo build.
            duration, fp_str = _compute_with_fpcalc(path)
    st = path.stat()
    bitrate = _safe_bitrate(path)
    return Fingerprint(
        path=path,
        duration=duration,
        fp_str=fp_str,
        size=st.st_size,
        mtime=st.st_mtime,
        bitrate=bitrate,
    )


def _safe_bitrate(path: Path) -> int | None:
    """Return bitrate in kbps, or ``None`` if the tag reader cannot decode."""
    from apps.shared import _tagreader

    if not _tagreader.HAS_TAG_READER:
        return None
    try:
        kbps = _tagreader.read(path).bitrate
    except _tagreader.TagReadError:
        return None
    return int(kbps) if kbps else None


# ---------------------------------------------------------------- compare


def _unpack(data: bytes, bits: int, count: int | None = None) -> list[int]:
    """Little-endian ``bits``-wide values packed into ``data``."""
    out: list[int] = []
    acc = n = 0
    mask = (1 << bits) - 1
    for byte in data:
        acc |= byte << n
        n += 8
        while n >= bits:
            out.append(acc & mask)
            acc >>= bits
            n -= bits
            if count is not None and len(out) == count:
                return out
    return out


def _pack(values: Sequence[int], bits: int) -> bytes:
    out = bytearray()
    acc = n = 0
    for v in values:
        acc |= v << n
        n += bits
        while n >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            n -= 8
    if n:
        out.append(acc & 0xFF)
    return bytes(out)


def _normal_extent(normal: list[int], count: int) -> tuple[int, int]:
    """How many 3-bit values encode ``count`` sub-fingerprints (each ends in
    a 0), and how many of those escape into a 5-bit exception value."""
    zeros = exceptions = 0
    for used, v in enumerate(normal, start=1):
        if v == 7:
            exceptions += 1
        elif v == 0:
            zeros += 1
            if zeros == count:
                return used, exceptions
    if count == 0:
        return 0, 0
    raise ValueError("not a chromaprint fingerprint: truncated body")


def decode_fingerprint(fp_str: str) -> tuple[list[int], int]:
    """Decompress a chromaprint string into ``(sub_fingerprints, algorithm)``.

    Port of chromaprint's ``FingerprintDecompressor``: a 4-byte header
    (algorithm, 24-bit big-endian count), then per sub-fingerprint the gaps
    between set bits of its XOR with the previous one, as 3-bit values
    (7 = escape into a 5-bit exception value) ending in a 0.

    Raises ``ValueError`` on a string that is not a valid fingerprint.
    """
    try:
        raw = base64.urlsafe_b64decode(fp_str + "=" * (-len(fp_str) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"not a chromaprint fingerprint: {exc}") from exc
    if len(raw) < 4:
        raise ValueError("not a chromaprint fingerprint: too short")
    algorithm = raw[0]
    count = int.from_bytes(raw[1:4], "big")
    normal = _unpack(raw[4:], 3)
    used, exceptions = _normal_extent(normal, count)
    exc_vals = _unpack(raw[4 + (used * 3 + 7) // 8 :], 5, exceptions)
    if len(exc_vals) != exceptions:
        raise ValueError("not a chromaprint fingerprint: truncated exceptions")
    words: list[int] = []
    cur = bit = ei = 0
    for packed in normal[:used]:
        gap = packed
        if packed == 7:
            gap += exc_vals[ei]
            ei += 1
        if gap == 0:
            words.append(cur)
            cur = bit = 0
            continue
        bit += gap
        if bit > 32:
            raise ValueError("not a chromaprint fingerprint: bit index past 32")
        cur |= 1 << (bit - 1)
    for i in range(1, len(words)):
        words[i] ^= words[i - 1]
    return words, algorithm


def encode_fingerprint(words: Sequence[int], algorithm: int = 1) -> str:
    """Compress sub-fingerprints the way ``fpcalc`` prints them (inverse of
    :func:`decode_fingerprint`)."""
    normal: list[int] = []
    exceptional: list[int] = []
    prev = 0
    for w in words:
        x = (w ^ prev) & 0xFFFFFFFF
        prev = w & 0xFFFFFFFF
        last = 0
        for b in range(1, 33):
            if x >> (b - 1) & 1:
                gap = b - last
                if gap >= 7:
                    normal.append(7)
                    exceptional.append(gap - 7)
                else:
                    normal.append(gap)
                last = b
        normal.append(0)
    raw = (
        bytes([algorithm & 0xFF])
        + len(words).to_bytes(3, "big")
        + _pack(normal, 3)
        + _pack(exceptional, 5)
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# Bounded: a 120 s fingerprint decodes to ~950 ints (~35 KB as a tuple).
@functools.lru_cache(maxsize=512)
def _decode(fp_str: str) -> tuple[int, ...]:
    return tuple(decode_fingerprint(fp_str)[0])


def words(fp: Fingerprint | str) -> tuple[int, ...]:
    """The decoded 32-bit sub-fingerprints of ``fp`` (cached)."""
    return _decode(fp.fp_str if isinstance(fp, Fingerprint) else fp)


# Fewer overlapping sub-fingerprints than this (~4 s of audio) cannot say
# two files are the same recording: short or near-silent files would match
# anything with the same few silence words.
MIN_OVERLAP_WORDS = 32


def compare(a: Fingerprint | str, b: Fingerprint | str, *, offset: int = 0) -> float:
    """Return similarity in ``[0.0, 1.0]`` for two fingerprints.

    Uses bitwise Hamming distance over the 32-bit sub-fingerprints.
    ``offset`` shifts ``b`` against ``a`` (sub-fingerprint ``i`` of ``a``
    meets ``i - offset`` of ``b``), for files whose audio starts at
    different points (encoder delay, trimmed silence). Unless the two
    strings are identical, the overlap must be at least
    :data:`MIN_OVERLAP_WORDS`, else the answer is 0.0.
    """
    sa = a.fp_str if isinstance(a, Fingerprint) else a
    sb = b.fp_str if isinstance(b, Fingerprint) else b
    if sa == sb and offset == 0:
        # Bit-identical chromaprint output, however short (a re-upload of
        # the same file): the overlap guard is for partial agreement.
        return 1.0
    wa = _decode(sa)
    wb = _decode(sb)
    if offset >= 0:
        wa = wa[offset:]
    else:
        wb = wb[-offset:]
    n = min(len(wa), len(wb))
    if n < MIN_OVERLAP_WORDS:
        return 0.0
    diff = 0
    for x, y in zip(wa[:n], wb[:n], strict=False):
        diff += (x ^ y).bit_count()
    return 1.0 - (diff / (n * 32))


def best_offset(a: Fingerprint | str, b: Fingerprint | str) -> int:
    """The shift of ``b`` against ``a`` that the most identical
    sub-fingerprints agree on (0 when none are shared)."""
    wa, wb = words(a), words(b)
    first_b: dict[int, int] = {}
    for pos, w in enumerate(wb):
        first_b.setdefault(w, pos)
    votes: dict[int, int] = {}
    for pos, w in enumerate(wa):
        pb = first_b.get(w)
        if pb is not None:
            votes[pos - pb] = votes.get(pos - pb, 0) + 1
    if not votes:
        return 0
    return max(votes.items(), key=lambda kv: (kv[1], -abs(kv[0])))[0]


def match(a: Fingerprint | str, b: Fingerprint | str) -> tuple[float, int]:
    """``(similarity, offset)`` at the better of no shift and the shift the
    shared sub-fingerprints vote for."""
    off = best_offset(a, b)
    at_zero = compare(a, b)
    if off == 0:
        return at_zero, 0
    shifted = compare(a, b, offset=off)
    return (shifted, off) if shifted > at_zero else (at_zero, 0)


# ---------------------------------------------------------------- cache


_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS fingerprints (
    path         TEXT PRIMARY KEY,
    stable_id    TEXT,
    fingerprint  TEXT NOT NULL,
    duration     REAL NOT NULL,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    bitrate      INTEGER,
    computed_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_fingerprints_stable_id
    ON fingerprints(stable_id);
"""


class FingerprintCache:
    """Thin SQLite wrapper backing ``load_or_compute``.

    The cache key is ``(abs_path, size, mtime)``. A row that was cached
    with different size/mtime is considered stale and overwritten on the
    next compute.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_CACHE_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def get(self, path: Path) -> Fingerprint | None:
        """Return a cached :class:`Fingerprint` if size + mtime match."""
        st = path.stat()
        with self._conn() as c:
            row = c.execute(
                "SELECT fingerprint, duration, size, mtime, bitrate, computed_at "
                "FROM fingerprints WHERE path = ?",
                (str(path),),
            ).fetchone()
        if row is None:
            return None
        fp_str, duration, size, mtime, bitrate, computed_at = row
        if size != st.st_size or abs(mtime - st.st_mtime) > 1e-3:
            return None
        if bitrate is None and not fs_residency.is_dataless_stub(st):
            # Rows cached while no tag reader was installed (the packaged app
            # before tinytag, Thu 1 Oct 2026) hold a NULL bitrate, which
            # canonical selection reads as 0 and so can keep the worse twin.
            # Backfill it here: the scan calls get() for every file, so the
            # cache heals on the next scan without re-running fpcalc. An
            # iCloud placeholder is skipped: opening it would download it.
            bitrate = _safe_bitrate(path)
            if bitrate is not None:
                with self._conn() as c:
                    c.execute(
                        "UPDATE fingerprints SET bitrate = ? WHERE path = ?",
                        (bitrate, str(path)),
                    )
                    c.commit()
        return Fingerprint(
            path=path,
            duration=float(duration),
            fp_str=fp_str,
            size=int(size),
            mtime=float(mtime),
            bitrate=int(bitrate) if bitrate is not None else None,
            computed_at=computed_at,
        )

    def put(self, fp: Fingerprint, *, stable_id: str | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO fingerprints "
                "(path, stable_id, fingerprint, duration, size, mtime, bitrate, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(path) DO UPDATE SET "
                "  stable_id=excluded.stable_id, "
                "  fingerprint=excluded.fingerprint, "
                "  duration=excluded.duration, "
                "  size=excluded.size, "
                "  mtime=excluded.mtime, "
                "  bitrate=excluded.bitrate, "
                "  computed_at=CURRENT_TIMESTAMP",
                (
                    str(fp.path),
                    stable_id,
                    fp.fp_str,
                    fp.duration,
                    fp.size,
                    fp.mtime,
                    fp.bitrate,
                ),
            )
            c.commit()

    def iter_all(self) -> Iterable[Fingerprint]:
        with self._conn() as c:
            for row in c.execute(
                "SELECT path, fingerprint, duration, size, mtime, bitrate, computed_at "
                "FROM fingerprints"
            ):
                path, fp_str, duration, size, mtime, bitrate, computed_at = row
                yield Fingerprint(
                    path=Path(path),
                    duration=float(duration),
                    fp_str=fp_str,
                    size=int(size),
                    mtime=float(mtime),
                    bitrate=int(bitrate) if bitrate is not None else None,
                    computed_at=computed_at,
                )


def load_or_compute(
    path: Path, cache: FingerprintCache, *, force: bool = False
) -> Fingerprint:
    """Cache-aware compute. Returns a cached hit when available."""
    if not force:
        hit = cache.get(path)
        if hit is not None:
            return hit
    fp = compute(path)
    cache.put(fp)
    return fp


__all__ = [
    "MIN_OVERLAP_WORDS",
    "ChromaprintMissing",
    "Fingerprint",
    "FingerprintCache",
    "FingerprintFailed",
    "best_offset",
    "compare",
    "compute",
    "decode_fingerprint",
    "encode_fingerprint",
    "load_or_compute",
    "match",
    "words",
]
