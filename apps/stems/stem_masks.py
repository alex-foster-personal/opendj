"""Stems stored as the source file plus compact separation masks (STEMSTORE-01, prototype).

Spike: ``(private path)``. A 4-stem bundle today stores four
lossy parts, about 2.5x a 128 kbps source at the 64/96 kbps size-policy rates,
and those parts are tinny (8-16 kHz set null about 4.7 dB). The source audio is
already on disk, so this format stores only what the source does not hold:

  * ``<stem>.low.opus``: the drums and bass parts below 2 kHz, waveform coded
    (Ogg Opus, ~32 kbps, written by libsndfile, no ffmpeg). Kick and bass share
    STFT bins, which a mask cannot split; a waveform can.
  * ``masks.odjm``: one soft mask per stem over a 2048-point STFT of the RESIDUAL
    (the source minus the decoded low parts), fitted to each stem's own residual
    (the lossless part minus its decoded low part), pooled to 128 bands x 2
    frames, 16 levels, lzma. Three masks are stored; the fourth is one minus
    their sum. So every stem gets its own low-band codec error back, and the four
    parts add back to the source EXACTLY.

Reconstruction (the deck's decode step) is: decode the source and the two low
parts, subtract, one STFT of that residual, four masked inverse STFTs, add each
stem's low part back. No crossover filter runs at decode time.

    python -m apps.stems.stem_masks encode <bundle_dir> --source <audio> --out <dir>
    python -m apps.stems.stem_masks check <out_dir> --bundle <bundle_dir> --source <audio>

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 The parts reconstructed from the source sum back to the source.
      [if] the four reconstructed parts are summed [then ⛔️] they null under 60 dB against the source
      [if] the header names low parts and none are given [then ⛔️] reconstruct proceeds
  ✔︎ ✅ 🎯 The mask payload is self-describing and refuses a mismatch.
      [if] a header field is changed [then ⛔️] decode returns instead of raising
      [if] the source has a different frame count than the encoded one [then ⛔️] reconstruct proceeds
      [if] the magic or version is wrong [then ⛔️] decode proceeds
  ✔︎ ✅ 🎯 Masks separate what they can see.
      [if] two stems occupy disjoint bands [then ⛔️] either reconstructs under 20 dB
      [if] a stem is silent [then ⛔️] its reconstruction holds more than -40 dB of the mix
  ✔︎ ✅ The low parts are sample aligned after the Opus round trip.
      [if] a decoded low part is shifted against its source [then ⛔️] the test passes
"""

from __future__ import annotations

import argparse
import io
import json
import lzma
import struct
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path

import numpy as np


# -----------------------------------------------------------------------------
# CFG
# -----------------------------------------------------------------------------
class CFG:
    MAGIC = b"ODJM"
    VERSION = 1
    STEMS: tuple[str, ...] = ("vocals", "drums", "bass", "other")
    DERIVED = "other"  # its mask is one minus the sum of the stored three
    NFFT = 2048
    HOP = 512
    POWER = 1.0  # amplitude-ratio mask: beat the power (Wiener) mask by 4-7 dB above 8 kHz (round 3)
    BANDS = 128
    LINEAR_BINS = 16  # every bin under ~340 Hz is its own band
    T_POOL = 2
    LEVELS = 16
    BLOCK_FRAMES = 1024  # STFT frames per processing block (bounds memory on a 10 min track)
    # Round 7 (masks over the residual): 2 kHz / 32 kbps drums+bass is 0.70x a 128 kbps source and
    # gives the bass solo 26.1 dB under 150 Hz (today's 64/96 kbps MP3 parts: 25.3 dB at 2.5x).
    LOW_FC_HZ = 2000.0
    LOW_CODED: tuple[str, ...] = ("drums", "bass")
    LOW_KBPS = 32.0
    LOW_RATE = 48000  # libsndfile writes Opus only at 8/12/16/24/48 kHz
    # libsndfile 1.2.2 Opus: kbps = 514.5 - 501.0 * level for a stereo 48 kHz stream
    # (measured Wed 4 Nov 2026: level 0.9 -> 63.8, 0.8 -> 114.1, 1.0 -> 13.5 kbps).
    OPUS_KBPS_AT_LEVEL_0 = 514.5
    OPUS_KBPS_PER_LEVEL = 501.0
    BUTTER_ORDER = 8


class MaskFormatError(ValueError):
    """A mask payload that this decoder cannot or must not apply."""


@dataclass(frozen=True)
class MaskHeader:
    """Everything the decoder checks or uses. ``power``, ``low_fc_hz`` and ``low_kbps`` are encoder
    provenance only (how the masks and low parts were made); decode never needs them."""

    version: int
    sample_rate: int
    source_frames: int
    nfft: int
    hop: int
    power: float
    band_edges: list[int]
    t_pool: int
    levels: int
    stems: list[str]
    stored: list[str]
    derived: str
    stft_frames: int
    low_fc_hz: float
    low_coded: list[str]
    low_kbps: float


# -----------------------------------------------------------------------------
# STFT with explicit framing (the layout a JS port must reproduce)
# -----------------------------------------------------------------------------
def _window(nfft: int) -> np.ndarray:
    return (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(nfft) / nfft)).astype(np.float32)  # periodic Hann


def _padded(x: np.ndarray, nfft: int, hop: int) -> tuple[np.ndarray, int]:
    """Pad nfft/2 zeros at the front and enough at the end that the frames tile the signal."""
    total = len(x) + nfft
    n_frames = 1 + int(np.ceil((total - nfft) / hop))
    back = (n_frames - 1) * hop + nfft - len(x) - nfft // 2
    return np.pad(x, ((nfft // 2, back), (0, 0))), n_frames


def _stft_block(xp: np.ndarray, f0: int, f1: int, nfft: int, hop: int, win: np.ndarray) -> np.ndarray:
    """Frames f0..f1 of a padded (n, ch) signal -> complex (frames, ch, bins)."""
    seg = xp[f0 * hop : (f1 - 1) * hop + nfft]
    frames = np.lib.stride_tricks.sliding_window_view(seg, nfft, axis=0)[::hop]  # (frames, ch, nfft)
    return np.fft.rfft(frames * win, axis=-1)


# -----------------------------------------------------------------------------
# Mask quantization
# -----------------------------------------------------------------------------
def band_edges(n_bins: int, n_bands: int, linear_bins: int) -> list[int]:
    """Bins under linear_bins are their own band; log-spaced bands above, ending at n_bins."""
    rest = np.round(np.geomspace(linear_bins, n_bins, n_bands - linear_bins + 1)).astype(int)
    edges = np.unique(np.concatenate([np.arange(linear_bins), rest]))
    return [int(e) for e in edges]


def _pool_masks(masks: np.ndarray, edges: list[int], t_pool: int) -> np.ndarray:
    """(stem, frames, bins) -> (stem, groups, bands), then renormalized to sum 1 per tile."""
    s, f, b = masks.shape
    pad = (-f) % t_pool
    m = np.pad(masks, ((0, 0), (0, pad), (0, 0)), mode="edge").reshape(s, -1, t_pool, b).mean(axis=2)
    pooled = np.stack([m[:, :, a:z].mean(axis=2) for a, z in pairwise(edges)], axis=2)
    return pooled / np.maximum(pooled.sum(axis=0, keepdims=True), 1e-9)


def _expand_masks(q: np.ndarray, header: MaskHeader, g0: int, g1: int, f0: int, f1: int) -> np.ndarray:
    """Quantized stored masks for groups g0..g1 -> full (stem, frames f0..f1, bins), summing to 1."""
    deq = q[:, g0:g1].astype(np.float32) / (header.levels - 1)
    last = np.clip(1.0 - deq.sum(axis=0, keepdims=True), 0.0, 1.0)
    full = np.concatenate([deq, last], axis=0)
    full /= np.maximum(full.sum(axis=0, keepdims=True), 1e-9)
    widths = np.diff(header.band_edges)
    per_bin = np.repeat(full, widths, axis=2)
    per_frame = np.repeat(per_bin, header.t_pool, axis=1)
    start = f0 - g0 * header.t_pool
    return per_frame[:, start : start + (f1 - f0)]


# -----------------------------------------------------------------------------
# Encode / decode the mask payload
# -----------------------------------------------------------------------------
def encode_masks(
    source: np.ndarray, parts: dict[str, np.ndarray], sample_rate: int, low_coded: tuple[str, ...] = ()
) -> bytes:
    """Masks fitted to ``parts`` over the STFT of ``source`` (the residual mixture and its targets
    when low parts are coded, see ``encode_bundle``). ``low_coded`` names the low parts the decoder
    must add back."""
    if set(parts) != set(CFG.STEMS):
        raise MaskFormatError(f"need parts {CFG.STEMS}, got {sorted(parts)}")
    n = len(source)
    win = _window(CFG.NFFT)
    padded = {s: _padded(_fit(parts[s], n), CFG.NFFT, CFG.HOP)[0] for s in CFG.STEMS}
    _, n_frames = _padded(source, CFG.NFFT, CFG.HOP)
    edges = band_edges(CFG.NFFT // 2 + 1, CFG.BANDS, CFG.LINEAR_BINS)
    stored = [s for s in CFG.STEMS if s != CFG.DERIVED]
    block = CFG.BLOCK_FRAMES - CFG.BLOCK_FRAMES % CFG.T_POOL
    chunks: list[np.ndarray] = []
    for f0 in range(0, n_frames, block):
        f1 = min(n_frames, f0 + block)
        mags = np.stack(
            [np.abs(_stft_block(padded[s], f0, f1, CFG.NFFT, CFG.HOP, win)) ** CFG.POWER for s in CFG.STEMS]
        )  # (stem, frames, ch, bins)
        mono = mags.sum(axis=2)  # one mask for both channels
        masks = mono / np.maximum(mono.sum(axis=0, keepdims=True), 1e-12)
        pooled = _pool_masks(masks, edges, CFG.T_POOL)
        chunks.append(np.round(pooled[[CFG.STEMS.index(s) for s in stored]] * (CFG.LEVELS - 1)).astype(np.int16))
    q = np.concatenate(chunks, axis=1)  # (stored, groups, bands)
    header = MaskHeader(
        CFG.VERSION,
        sample_rate,
        n,
        CFG.NFFT,
        CFG.HOP,
        CFG.POWER,
        edges,
        CFG.T_POOL,
        CFG.LEVELS,
        list(CFG.STEMS),
        stored,
        CFG.DERIVED,
        n_frames,
        CFG.LOW_FC_HZ,
        list(low_coded),
        CFG.LOW_KBPS,
    )
    delta = np.diff(q, axis=1, prepend=0).astype(np.int8)  # time deltas: mostly 0, compress hard
    payload = lzma.compress(delta.tobytes(), preset=9 | lzma.PRESET_EXTREME)
    head = json.dumps(asdict(header), separators=(",", ":")).encode()
    shape = struct.pack("<III", *q.shape)
    return CFG.MAGIC + struct.pack("<HI", CFG.VERSION, len(head)) + head + shape + payload


def decode_masks(blob: bytes) -> tuple[MaskHeader, np.ndarray]:
    if blob[:4] != CFG.MAGIC:
        raise MaskFormatError("not an Open DJ stem mask file (bad magic)")
    version, head_len = struct.unpack_from("<HI", blob, 4)
    if version != CFG.VERSION:
        raise MaskFormatError(f"mask format version {version}, this decoder reads {CFG.VERSION}")
    at = 10
    header = MaskHeader(**json.loads(blob[at : at + head_len]))
    if [*header.stored, header.derived] != header.stems:
        raise MaskFormatError(
            f"header stems {header.stems} must be the stored masks {header.stored} then the derived {header.derived!r}"
        )
    if not set(header.low_coded) <= set(header.stems):
        raise MaskFormatError(f"header low parts {header.low_coded} are not stems {header.stems}")
    at += head_len
    shape = struct.unpack_from("<III", blob, at)
    delta = np.frombuffer(lzma.decompress(blob[at + 12 :]), dtype=np.int8).reshape(shape)
    q = np.cumsum(delta.astype(np.int16), axis=1)
    expected = (len(header.stored), -(-header.stft_frames // header.t_pool), len(header.band_edges) - 1)
    if q.shape != expected or int(np.min(q)) < 0 or int(np.max(q)) > header.levels - 1:
        raise MaskFormatError(f"mask payload shape {q.shape} or range does not match its header {expected}")
    return header, q


# -----------------------------------------------------------------------------
# Low band: waveform-coded parts below the crossover
# -----------------------------------------------------------------------------
def _lowpass(x: np.ndarray, fc: float, rate: int) -> np.ndarray:
    """Zero-phase Butterworth; x - _lowpass(x) is its exact complementary high band."""
    from scipy.signal import butter, sosfiltfilt

    sos = butter(CFG.BUTTER_ORDER, fc, btype="low", fs=rate, output="sos")
    return sosfiltfilt(sos, x, axis=0).astype(np.float32)


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(src, dst)
    return resample_poly(x, dst // g, src // g, axis=0).astype(np.float32)


def opus_level_for_kbps(kbps: float) -> float:
    level = (CFG.OPUS_KBPS_AT_LEVEL_0 - kbps) / CFG.OPUS_KBPS_PER_LEVEL
    if not 0.0 <= level < 1.0:
        raise ValueError(f"{kbps} kbps is outside libsndfile's Opus range")
    return level


def encode_low_part(part: np.ndarray, sample_rate: int) -> bytes:
    import soundfile as sf

    low = _resample(_lowpass(part, CFG.LOW_FC_HZ, sample_rate), sample_rate, CFG.LOW_RATE)
    buf = io.BytesIO()
    sf.write(buf, low, CFG.LOW_RATE, format="OGG", subtype="OPUS", compression_level=opus_level_for_kbps(CFG.LOW_KBPS))
    return buf.getvalue()


def decode_low_part(blob: bytes, sample_rate: int, frames: int) -> np.ndarray:
    import soundfile as sf

    data, rate = sf.read(io.BytesIO(blob), dtype="float32", always_2d=True)
    return _fit(_resample(data, rate, sample_rate), frames)


def encode_bundle(source: np.ndarray, parts: dict[str, np.ndarray], sample_rate: int) -> tuple[bytes, dict[str, bytes]]:
    """The mask payload and the coded low parts for one track.

    The masks are fitted AFTER the low parts round-trip through the codec, against what the
    decoder will hold: the residual mixture (source minus decoded lows) and each stem's residual
    target (lossless part minus its decoded low). Round 7: this beats deriving one stem's low band
    by 11 dB full band on that stem, at the same size.
    """
    n = len(source)
    low_blobs = {s: encode_low_part(_fit(parts[s], n), sample_rate) for s in CFG.LOW_CODED}
    lows = {s: decode_low_part(blob, sample_rate, n) for s, blob in low_blobs.items()}
    residual = source - _sum(lows.values()) if lows else source
    targets = {s: _fit(parts[s], n) - lows[s] if s in lows else _fit(parts[s], n) for s in CFG.STEMS}
    return encode_masks(residual, targets, sample_rate, CFG.LOW_CODED), low_blobs


# -----------------------------------------------------------------------------
# Reconstruction (the deck's decode step)
# -----------------------------------------------------------------------------
def _fit(x: np.ndarray, frames: int) -> np.ndarray:
    x = x[:, :2] if x.shape[1] >= 2 else np.repeat(x, 2, axis=1)
    if len(x) < frames:
        return np.concatenate([x, np.zeros((frames - len(x), 2), np.float32)])
    return x[:frames]


def _overlap_add(frames: np.ndarray, hop: int) -> np.ndarray:
    """(frames, ch, nfft) -> (samples, ch). Frames r, r+k, r+2k (k = nfft / hop) tile without overlap."""
    n_frames, ch, nfft = frames.shape
    stride = nfft // hop
    out = np.zeros(((n_frames - 1) * hop + nfft, ch), np.float32)
    for r in range(stride):
        sel = frames[r::stride].transpose(0, 2, 1).reshape(-1, ch)
        out[r * hop : r * hop + len(sel)] += sel
    return out


def reconstruct_parts(
    source: np.ndarray, sample_rate: int, blob: bytes, lows: dict[str, np.ndarray] | None = None
) -> dict[str, np.ndarray]:
    """Four parts from the decoded source (at ``sample_rate``), the mask payload and the decoded low parts."""
    header, q = decode_masks(blob)
    if sample_rate != header.sample_rate:
        raise MaskFormatError(f"source decodes at {sample_rate} Hz, masks were made at {header.sample_rate} Hz")
    if len(source) != header.source_frames:
        raise MaskFormatError(f"source has {len(source)} frames, masks were made for {header.source_frames}")
    lows = lows or {}
    if set(lows) != set(header.low_coded):
        raise MaskFormatError(f"low parts {sorted(lows)} do not match the header's {header.low_coded}")
    fitted_lows = {s: _fit(lows[s], len(source)) for s in lows}
    residual = _fit(source, len(source)) - (_sum(fitted_lows.values()) if fitted_lows else 0.0)
    win = _window(header.nfft)
    xp, n_frames = _padded(residual, header.nfft, header.hop)
    out = {s: np.zeros_like(xp) for s in header.stems}
    block = CFG.BLOCK_FRAMES - CFG.BLOCK_FRAMES % header.t_pool
    if header.nfft % header.hop:
        raise MaskFormatError(f"hop {header.hop} does not divide nfft {header.nfft}")
    for f0 in range(0, n_frames, block):
        f1 = min(n_frames, f0 + block)
        spec = _stft_block(xp, f0, f1, header.nfft, header.hop, win)  # (frames, ch, bins)
        masks = _expand_masks(q, header, f0 // header.t_pool, -(-f1 // header.t_pool), f0, f1)
        a = f0 * header.hop
        for i, stem in enumerate(header.stems):
            frames = (np.fft.irfft(spec * masks[i][:, None, :], n=header.nfft, axis=-1) * win).astype(np.float32)
            seg = _overlap_add(frames, header.hop)
            out[stem][a : a + len(seg)] += seg
    norm = _overlap_add(np.broadcast_to(win**2, (n_frames, 1, header.nfft)), header.hop)[:, 0]
    lo, hi = header.nfft // 2, header.nfft // 2 + header.source_frames
    scale = 1.0 / np.maximum(norm[lo:hi], 1e-6)[:, None]
    zero = np.zeros((header.source_frames, 2), np.float32)
    return {s: (out[s][lo:hi] * scale + fitted_lows.get(s, zero)).astype(np.float32) for s in header.stems}


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def _read(path: Path) -> tuple[np.ndarray, int]:
    import soundfile as sf

    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    return _fit(data, len(data)), rate


def _bundle_parts(bundle: Path, frames: int, rate: int) -> dict[str, np.ndarray]:
    files = json.loads((bundle / "manifest.json").read_text())["files"]
    parts = {}
    for stem in CFG.STEMS:
        data, part_rate = _read(bundle / files[stem])
        if part_rate != rate:
            raise MaskFormatError(f"{stem} is {part_rate} Hz, the source decodes at {rate} Hz")
        parts[stem] = _fit(data, frames)
    return parts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    enc = sub.add_parser("encode", help="write masks.odjm + low parts from a lossless bundle")
    enc.add_argument("bundle", type=Path)
    enc.add_argument("--source", type=Path, required=True)
    enc.add_argument("--out", type=Path, required=True)
    chk = sub.add_parser("check", help="reconstruct and print size and per-stem depth against the bundle")
    chk.add_argument("out", type=Path)
    chk.add_argument("--bundle", type=Path, required=True)
    chk.add_argument("--source", type=Path, required=True)
    args = ap.parse_args(argv)
    source, rate = _read(args.source)
    if args.cmd == "encode":
        parts = _bundle_parts(args.bundle, len(source), rate)
        args.out.mkdir(parents=True, exist_ok=True)
        masks_blob, low_blobs = encode_bundle(source, parts, rate)
        (args.out / "masks.odjm").write_bytes(masks_blob)
        for stem, blob in low_blobs.items():
            (args.out / f"{stem}.low.opus").write_bytes(blob)
        return 0
    stored = [args.out / "masks.odjm", *(args.out / f"{s}.low.opus" for s in CFG.LOW_CODED)]
    lows = {s: decode_low_part((args.out / f"{s}.low.opus").read_bytes(), rate, len(source)) for s in CFG.LOW_CODED}
    parts = reconstruct_parts(source, rate, stored[0].read_bytes(), lows)
    truth = _bundle_parts(args.bundle, len(source), rate)
    total = sum(p.stat().st_size for p in stored)
    report = {
        "stored_bytes": total,
        "size_x_source": round(total / args.source.stat().st_size, 3),
        "masks_bytes": stored[0].stat().st_size,
        "sum_null_db": round(_depth(source, _sum(parts.values())), 1),
        "stem_full_db": {s: round(_depth(truth[s], parts[s]), 2) for s in CFG.STEMS},
        "inst_full_db": round(
            _depth(
                _sum(truth[s] for s in CFG.STEMS if s != "vocals"), _sum(parts[s] for s in CFG.STEMS if s != "vocals")
            ),
            2,
        ),
    }
    print(json.dumps(report, indent=1))
    return 0


def _sum(arrays: Iterable[np.ndarray]) -> np.ndarray:
    return np.sum(np.stack(list(arrays)), axis=0)


def _depth(ref: np.ndarray, got: np.ndarray) -> float:
    return float(
        10 * np.log10(np.sum(ref.astype(np.float64) ** 2) / max(np.sum((ref - got).astype(np.float64) ** 2), 1e-20))
    )


if __name__ == "__main__":
    sys.exit(main())
