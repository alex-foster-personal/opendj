"""Encode one stem part in the size policy's codec, with no ffmpeg (STEM-57).

Shared by the separation worker (``scripts/stem_bundle_worker.py``) and the
re-compress pass over existing bundles (``apps.stems.recompress``), so a new
bundle and a re-compressed one cannot disagree on codec, rung or bitrate.

MP3 is encoded by the LAME inside soundfile's bundled libsndfile (soundfile
>= 0.13 bundles libsndfile 1.2), never by an ffmpeg subprocess: the installed
app ships no ffmpeg, and STEM-51's answer to that (lossless FLAC parts for a
lossy source) made every part 2.5x to 4.2x the source file. The codec choice
itself and the two-rung ladder belong to ``apps.stems.stem_size_policy``.

soundfile is imported lazily: it ships in the ``analysis`` and ``vocals``
extras, not in the base install.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from apps.stems import stem_size_policy as pol

# MPEG-1 Layer III bitrate bounds (44.1/48 kHz, the only stem rates), the
# range libsndfile spreads its 0..1 compression level over for CBR.
MP3_MPEG1_MAX_KBPS = 320
MP3_MPEG1_MIN_KBPS = 32
# libsndfile 1.2.2 refuses a compression level of exactly 1.0 for MP3 ("Error
# set compression level 1.0"), which is what the 32 kbps floor maps to. Any
# level from 0.986 up to just under 1.0 encodes at 32 kbps (measured Fri 9 Oct
# 2026, soundfile 0.13.1: 0.985, 0.99, 0.999 and 0.9999 all write 32 kbps
# frame headers; 0.972 writes 40), so the floor rung is asked for just inside.
SNDFILE_MP3_MAX_COMPRESSION = 0.999
# The one MPEG layer libsndfile can encode.
MP3_SUBTYPE = "MPEG_LAYER_III"


class Mp3EncoderUnavailableError(RuntimeError):
    """soundfile here cannot write MP3, so no lossy stem can be honest."""


def require_mp3_encoder(subtype: str = MP3_SUBTYPE) -> None:
    """Raise before any work when soundfile cannot write MP3.

    Without it there is no honest output for a lossy source: lossless FLAC
    makes every part larger than the source (the STEM-51 fallback this
    replaces), so the caller refuses instead of swapping codec. The format
    list alone is not proof: a libsndfile can list MP3 and still lack the
    Layer III encoder, so this writes a short real file in both modes the
    worker uses (Codex on #6820) before any model loads. ``subtype`` is the
    layer ``encode_mp3`` writes; only a test asks for another.
    """
    import soundfile as sf

    if "MP3" not in sf.available_formats():
        raise Mp3EncoderUnavailableError(
            f"soundfile {sf.__version__} (libsndfile {sf.__libsndfile_version__}) "
            "cannot write MP3; stems for an MP3/M4A source need soundfile >= 0.13 "
            "with its bundled libsndfile >= 1.1"
        )
    _probe_mp3_write(subtype)


def _probe_mp3_write(subtype: str) -> None:
    """Write 50 ms of silence as ``subtype`` at the CBR floor rung and at V0
    VBR, the two settings ``encode_mp3`` uses; any refusal is the error."""
    import tempfile

    import numpy as np
    import soundfile as sf

    silence = np.zeros((2205, 2), dtype="float32")
    with tempfile.TemporaryDirectory(prefix="odj-mp3-probe-") as tmp:
        for mode, level in (("CONSTANT", SNDFILE_MP3_MAX_COMPRESSION), ("VARIABLE", 0.0)):
            probe = Path(tmp) / f"probe-{mode.lower()}.mp3"
            try:
                sf.write(
                    str(probe),
                    silence,
                    44100,
                    format="MP3",
                    subtype=subtype,
                    compression_level=level,
                    bitrate_mode=mode,
                )
            except (sf.LibsndfileError, RuntimeError, ValueError) as exc:
                raise Mp3EncoderUnavailableError(
                    f"soundfile {sf.__version__} (libsndfile {sf.__libsndfile_version__}) "
                    f"lists MP3 but cannot write {subtype} ({mode} bitrate): {exc}"
                ) from exc
            if probe.stat().st_size == 0:
                raise Mp3EncoderUnavailableError(f"libsndfile wrote an empty {subtype} probe ({mode} bitrate)")


def sndfile_mp3_compression(cbr_kbps: int) -> float:
    """libsndfile's 0..1 compression level for one MPEG-1 Layer III CBR rate.

    libsndfile maps CBR compression c to ``320 - c * (320 - 32)`` kbps and
    LAME snaps that to the nearest standard rate, so the inverse lands exactly
    on any rate in the ladder (measured: 320/256/224/192/160/128 kbps encode
    at 320.3/256.2/224.2/192.2/160.1/128.1 kbps on a real 60 s mix). The 32
    kbps floor is capped at ``SNDFILE_MP3_MAX_COMPRESSION``; every rate is
    checked against the real encoder in
    tests/scripts/test_stem_bundle_worker_decode.py.
    """
    top, bottom = MP3_MPEG1_MAX_KBPS, MP3_MPEG1_MIN_KBPS
    if not bottom <= cbr_kbps <= top:
        raise ValueError(f"cbr_kbps must be within {bottom}..{top}, got {cbr_kbps}")
    return min((top - cbr_kbps) / (top - bottom), SNDFILE_MP3_MAX_COMPRESSION)


def encode_mp3(arr: Any, sample_rate: int, out_path: Path, *, cbr_kbps: int | None) -> bytes:
    """LAME-encode one [frames, channels] float array to mp3 at ``out_path``:
    CBR at ``cbr_kbps`` if given, else V0 VBR (VBR quality 0, the same rate
    ffmpeg's ``-q:a 0`` gives: 266.8 kbps both on the same 60 s mix).

    Written straight to ``out_path``, a real seekable file, so libsndfile
    patches the Xing/VBR header with the true frame count once encoding ends;
    a VBR mp3 without it reads the wrong duration in every fast reader.
    """
    import soundfile as sf

    if cbr_kbps is None:
        mode, level = "VARIABLE", 0.0
    else:
        mode, level = "CONSTANT", sndfile_mp3_compression(cbr_kbps)
    sf.write(
        str(out_path),
        arr,
        sample_rate,
        format="MP3",
        subtype=MP3_SUBTYPE,
        compression_level=level,
        bitrate_mode=mode,
    )
    return out_path.read_bytes()


def encode_flac_bytes(arr: Any, sample_rate: int) -> bytes:
    """One [frames, channels] float array as 16-bit FLAC bytes (house
    convention: control/eval FLAC is always PCM_16, never a source-bit-depth
    match -- see modal_vocal_farm.py's ``_encode_flac``)."""
    import soundfile as sf

    buffer = io.BytesIO()
    sf.write(buffer, arr, sample_rate, format="FLAC", subtype="PCM_16")
    return buffer.getvalue()


def encode_part(
    arr: Any,
    sample_rate: int,
    *,
    codec: str,
    part: str,
    mp3_settings: str | None,
    source_bytes: int,
    source_kbps: float,
    out_path: Path,
) -> tuple[bytes, str | None]:
    """Write one stem part at ``out_path`` in ``codec``; return (bytes,
    final mp3 CBR settings string, or None if rung 1 held / codec is flac).

    STEM-8231 (whole set <= track, 64 kbps floor): ``mp3_settings`` is the
    rung-1 whole-set CBR (``lame_<N>kbps_cbr``; None derives it with ``pol.
    mp3_lame_settings_for_source_kbps``) and the part should fit its quarter
    of the source (``pol.whole_set_part_budget_bytes``). LAME pads up to two
    frames, so rung 1 can land a few hundred bytes over: the encode then steps
    down the ladder one rate at a time (the safety rung), never under the
    part's floor (``pol.mp3_part_floor_kbps``: 96 kbps for bass and drums, 64
    for vocals and other; a rung 1 under it is lifted to it). If the part is
    still over at that floor it is KEPT at the floor: the maintainer chose those floors
    over the size rule. ``part`` is the stem name (vocals/drums/bass/other).
    """
    if codec == "flac":
        flac_bytes = encode_flac_bytes(arr, sample_rate)
        out_path.write_bytes(flac_bytes)
        return flac_bytes, None
    if codec != "mp3":
        raise ValueError(f"stem codec must be flac or mp3, got {codec!r}")

    budget = pol.whole_set_part_budget_bytes(source_bytes)
    rung1_cbr = pol.mp3_cbr_kbps_from_settings(mp3_settings or pol.mp3_lame_settings_for_source_kbps(source_kbps))
    for cbr in pol.mp3_whole_set_step_down_kbps(rung1_cbr, pol.mp3_part_floor_kbps(part)):
        encoded = encode_mp3(arr, sample_rate, out_path, cbr_kbps=cbr)
        if len(encoded) <= budget:
            break
    return encoded, (None if cbr == rung1_cbr else f"lame_{cbr}kbps_cbr")


__all__ = [
    "MP3_MPEG1_MAX_KBPS",
    "MP3_MPEG1_MIN_KBPS",
    "SNDFILE_MP3_MAX_COMPRESSION",
    "Mp3EncoderUnavailableError",
    "encode_flac_bytes",
    "encode_mp3",
    "encode_part",
    "require_mp3_encoder",
    "sndfile_mp3_compression",
]
