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


class StemPartOverSourceError(RuntimeError):
    """An MP3 part is still larger than its source at the lowest usable CBR
    rung, so it is not published (STEM-57)."""


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
    mp3_settings: str | None,
    source_bytes: int,
    source_kbps: float,
    out_path: Path,
) -> tuple[bytes, str | None]:
    """Write one stem part at ``out_path`` in ``codec``; return (bytes,
    final mp3 CBR settings string, or None if rung 1 held / codec is flac).

    An MP3 part larger than its source is never returned (STEM-57). When
    rung 1 is over, the part is re-encoded at the nearest standard CBR at or
    under the source rate (rung 2). LAME pads up to two frames and a source
    can sit a hair above a standard rate, so rung 2 can still land a few
    hundred bytes over: the encode then steps down ``MP3_CBR_LADDER`` one rate
    at a time until the part fits. If the lowest rate is still over, the
    oversized file is removed and ``StemPartOverSourceError`` names the part
    and both sizes. Both callers write into a private work directory, so a
    raise here happens before anything is published or swapped.

    This is stricter than ``stem_size_policy``'s batch check, which records an
    overage for routes that encode elsewhere; that check is unchanged.
    """
    if codec == "flac":
        flac_bytes = encode_flac_bytes(arr, sample_rate)
        out_path.write_bytes(flac_bytes)
        return flac_bytes, None
    if codec != "mp3":
        raise ValueError(f"stem codec must be flac or mp3, got {codec!r}")

    cbr_kbps = pol.MP3_CBR_KBPS if mp3_settings == "lame_320kbps_cbr" else None
    encoded = encode_mp3(arr, sample_rate, out_path, cbr_kbps=cbr_kbps)
    if not pol.lossy_stem_exceeds_source(source_bytes=source_bytes, part_bytes=len(encoded), codec=codec):
        return encoded, None

    rung2_cbr = pol.mp3_rung2_cbr_kbps(source_kbps)
    for cbr in pol.MP3_CBR_LADDER[pol.MP3_CBR_LADDER.index(rung2_cbr) :]:
        encoded = encode_mp3(arr, sample_rate, out_path, cbr_kbps=cbr)
        if not pol.lossy_stem_exceeds_source(source_bytes=source_bytes, part_bytes=len(encoded), codec=codec):
            return encoded, f"lame_{cbr}kbps_cbr"
    out_path.unlink()
    raise StemPartOverSourceError(
        f"stem part {out_path.name} is {len(encoded)} bytes at {pol.MP3_CBR_LADDER[-1]} kbps CBR, the lowest "
        f"MP3 rate, which is still over its {source_bytes}-byte source ({source_kbps:.2f} kbps); not published"
    )


__all__ = [
    "MP3_MPEG1_MAX_KBPS",
    "MP3_MPEG1_MIN_KBPS",
    "SNDFILE_MP3_MAX_COMPRESSION",
    "Mp3EncoderUnavailableError",
    "StemPartOverSourceError",
    "encode_flac_bytes",
    "encode_mp3",
    "encode_part",
    "require_mp3_encoder",
    "sndfile_mp3_compression",
]
