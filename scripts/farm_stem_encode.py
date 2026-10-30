"""The Modal stem farm's codec policy and encoders, mirrored from ``apps.stems``.

WHY A SEPARATE FILE. ``scripts/modal_vocal_farm.py`` runs inside a Modal
container that has no ``apps`` package, so it cannot import
``apps.stems.stem_size_policy`` (which codec, which MP3 rung) or
``apps.stems.stem_encode`` (LAME through soundfile, the two-rung ladder). It
keeps MIRRORS of both, and they live here rather than in the farm so the farm
module stays inside its size budget and this code can be tested without
modal. STEM-75: the farm writes MP3 stems for an MP3/M4A source through the
same ladder as the local worker (STEM-57).

HOW IT REACHES THE CONTAINER. The farm loads this file by path, as its own
sibling (``modal_vocal_farm._FARM_STEM_ENCODE``), and its image copies it to
``/root/farm_stem_encode.py`` (``add_local_file(..., copy=True)``), which is
where that sibling is when Modal runs the farm as a single file. Imported as
``scripts.modal_vocal_farm``, Modal mounts the whole ``scripts`` package and
the sibling is the package's copy. Either way it is this file's bytes.

PURE STDLIB at import time: numpy and soundfile are imported inside the
functions that encode, and ``apps`` only inside the two ``assert_*_mirror_*``
checks, which the farm runs LOCALLY before buying any GPU. The byte-level
equivalence with ``apps.stems.stem_encode`` is pinned by
``tests/stems/test_farm_mp3_ladder.py``.

-Claude
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# STEM CODEC. Stems are several times larger than their source, so the wire
# cost is worth choosing deliberately rather than inheriting.
#
# COUNT THE RIGHT POPULATION. state.db holds 8343 rows with a duration, but
# only 1617 point at a file that EXISTS -- the rest reference a machine that is
# gone. Costing the 8343 overstates everything by ~5x, and this comment exists
# because that mistake was made here first. The farmable set is 1617 tracks /
# 120.2 h audio: $7.94 at tier M, 19 min of GPU.
#
# Stem volume for THAT set, from a real Modal probe (one 2.50-min track, all
# four stems ffprobe'd): 53.17 MB flac vs 14.45 MB opus, 3.68x.
#   flac  153 GB  -> ~2.9 h of upload at the measured 14.6 MB/s
#   opus   42 GB  -> ~0.8 h
# With --stems-dest bifrost2 every byte crosses this Mac TWICE (down from
# Modal, up to bifrost2), which is what makes the choice matter at all: it is
# 2 h of the operator's evening, not a rounding error, and --stems-dest r2
# would remove the Mac from the path entirely (see issue #373).
#
# WHY LOSSY IS DEFENSIBLE FOR THIS PRODUCT, and where it is not: these stems
# drive deck mute/solo and live mashups, not mastering. The separation itself
# is vastly more destructive than any of these codecs, and most sources are
# already ~320 kbps MP3. Keep FLAC for the L rung and for anything that will be
# SCORED -- SI-SDR against a lossy stem measures the codec as well as the model.
STEM_CODECS: dict[str, dict[str, Any]] = {
    # ext, ffmpeg args, and the per-4.76-min-track size measured above
    "flac": {"ext": "flac", "args": None, "mb_per_track": 117},
    # AAC IS DELIBERATELY ABSENT. It was implemented, measured, and rejected.
    # m4a cannot be piped at all (MP4 needs a seekable output for its moov
    # atom). ADTS pipes fine, but MEASURED on a 152.020 s source the four
    # stems reported 165.564 / 154.541 / 163.806 / 162.642 s -- ADTS carries no
    # reliable duration header, so every stem disagrees with the mixture and
    # with the others by up to 11 s. For a product whose entire job is staying
    # sample-aligned under a deck, that is a footgun, not a tradeoff. Opus on
    # the same source reported 152.026 s, a 6 ms drift, and is smaller anyway.
    "opus": {
        "ext": "opus",
        # Default args are a fallback only. Prefer opus_ffmpeg_audio_args(source_kbps)
        # so bitrate follows the input (192k-per-stem was ~4x a 320k MP3).
        "args": ["-c:a", "libopus", "-b:a", "80k"],
        "mb_per_track": 12,  # ~80k x4 on a ~4 min track; was 27 at fixed 192k
    },
    # MP3 through the two-rung LAME ladder (STEM-57's, mirrored below from
    # apps.stems.stem_encode): each part at or under the source's own bitrate.
    # Not ffmpeg: libsndfile's LAME, the same encoder the local worker uses.
    "mp3": {"ext": "mp3", "args": None, "mb_per_track": None},  # unmeasured here
}
# THE DEFAULT IS THE SOURCE-FORMAT POLICY, not one codec (STEM-75, Fri 9 Oct
# 2026). Until then every farm route asked for FLAC, so an MP3 source came
# home as four lossless parts each 2.5x to 4.2x the source file -- the
# regression STEM-57 fixed for the local worker. "by-source" resolves per
# track exactly as apps.stems.stem_size_policy.stem_output_codec_for_source_ext
# does: lossless source -> FLAC, MP3/M4A source -> MP3 via the ladder, any
# other extension -> a per-track error before GPU time is spent. A caller that
# wants FLAC regardless (tier L, anything that will be scored) names "flac".
STEM_CODEC_BY_SOURCE: str = "by-source"

# MIRRORED from apps/stems/stem_size_policy.py -- the GPU container has no
# ``apps`` package. assert_stem_size_policy_mirror_matches() fails locally if
# the two drift.
STEM_PART_COUNT: int = 4
OPUS_MIN_KBPS: int = 48
OPUS_MAX_KBPS: int = 128
LOSSY_SIZE_GATED: frozenset[str] = frozenset({"opus", "mp3"})
CONTROL_CODECS: frozenset[str] = frozenset({"flac", "wav"})
# The source-format codec policy and the MP3 ladder (stem_size_policy.py), and
# libsndfile's MPEG-1 CBR bounds (apps/stems/stem_encode.py). Same check.
LOSSLESS_SOURCE_EXTS: frozenset[str] = frozenset({".flac", ".wav", ".aif", ".aiff"})
LOSSY_SOURCE_EXTS: frozenset[str] = frozenset({".mp3", ".m4a"})
MP3_CBR_KBPS: int = 320
# Every MPEG-1 Layer III rate down to the format's 32 kbps floor, so a
# source under 192 kbps (a 128 kbps rip) gets a rung-2 CBR instead of a
# raise after the GPU time is spent (Sentry on #6203).
MP3_CBR_LADDER: tuple[int, ...] = (320, 256, 224, 192, 160, 128, 112, 96, 80, 64, 56, 48, 40, 32)
MP3_MPEG1_MAX_KBPS: int = 320
MP3_MPEG1_MIN_KBPS: int = 32
# Mirror of stem_encode.SNDFILE_MP3_MAX_COMPRESSION: libsndfile 1.2.2 refuses
# a compression level of exactly 1.0, so the 32 kbps floor is asked for just
# inside it.
SNDFILE_MP3_MAX_COMPRESSION: float = 0.999


def effective_bitrate_kbps(size_bytes: int, duration_s: float) -> float:
    if size_bytes <= 0:
        raise ValueError(f"size_bytes must be positive, got {size_bytes}")
    if duration_s <= 0:
        raise ValueError(f"duration_s must be positive, got {duration_s}")
    return size_bytes * 8 / duration_s / 1000


def opus_kbps_per_stem(source_kbps: float) -> int:
    if source_kbps <= 0:
        raise ValueError(f"source_kbps must be positive, got {source_kbps}")
    raw = round(source_kbps / STEM_PART_COUNT)
    return max(OPUS_MIN_KBPS, min(OPUS_MAX_KBPS, raw))


def opus_ffmpeg_audio_args(source_kbps: float) -> list[str]:
    kbps = opus_kbps_per_stem(source_kbps)
    return ["-c:a", "libopus", "-b:a", f"{kbps}k"]


def assert_lossy_stems_not_larger_than_source(
    *,
    source_bytes: int,
    part_sizes: dict[str, int],
    codec: str,
) -> None:
    if codec in CONTROL_CODECS or codec not in LOSSY_SIZE_GATED:
        return
    if source_bytes <= 0:
        raise RuntimeError(f"source_bytes must be positive, got {source_bytes}")
    offenders = [f"{n}={s}B" for n, s in part_sizes.items() if s > source_bytes]
    if offenders:
        raise RuntimeError(
            f"lossy stem part(s) exceed source ({source_bytes}B) under codec "
            f"{codec!r}: {', '.join(offenders)}. Encode bitrate must follow "
            f"source (see opus_kbps_per_stem)."
        )


def stem_output_codec_for_source_ext(source_ext: str) -> str:
    """Mirror of stem_size_policy.stem_output_codec_for_source_ext: "flac" for
    a lossless source, "mp3" for a lossy one, raise on anything else."""
    ext = source_ext.lower()
    if ext in LOSSLESS_SOURCE_EXTS:
        return "flac"
    if ext in LOSSY_SOURCE_EXTS:
        return "mp3"
    raise ValueError(
        f"no output-format policy for source extension {source_ext!r}; known "
        f"lossless={sorted(LOSSLESS_SOURCE_EXTS)}, lossy={sorted(LOSSY_SOURCE_EXTS)}"
    )


def resolve_stem_codec(stem_codec: str, source_name: str) -> str:
    """The codec every part of THIS track is written in.

    ``by-source`` (the default, STEM-75) follows the source-format policy; any
    key of STEM_CODECS is taken as asked, so a caller that names ``flac`` gets
    FLAC whatever the source. Runs before the model loads, so an unknown codec
    or an extension with no policy costs no GPU time.
    """
    if stem_codec == STEM_CODEC_BY_SOURCE:
        return stem_output_codec_for_source_ext(Path(source_name).suffix)
    if stem_codec not in STEM_CODECS:
        raise RuntimeError(
            f"unknown stem codec {stem_codec!r}; known: "
            f"{', '.join(sorted([*STEM_CODECS, STEM_CODEC_BY_SOURCE]))}"
        )
    return stem_codec


def mp3_lame_settings_for_source_kbps(source_kbps: float) -> str:
    """Mirror of stem_size_policy.mp3_lame_settings_for_source_kbps (rung 1)."""
    if source_kbps <= 0:
        raise ValueError(f"source_kbps must be positive, got {source_kbps}")
    return "lame_320kbps_cbr" if source_kbps >= MP3_CBR_KBPS else "lame_v0_vbr"


def mp3_rung2_cbr_kbps(source_kbps: float) -> int:
    """Mirror of stem_size_policy.mp3_rung2_cbr_kbps: the nearest ladder CBR
    at or under floor(source_kbps); raise when even the floor rung does not
    fit (a policy gap, never a guessed bitrate)."""
    import math

    if source_kbps <= 0:
        raise ValueError(f"source_kbps must be positive, got {source_kbps}")
    ceiling = math.floor(source_kbps)
    for cbr in MP3_CBR_LADDER:
        if cbr <= ceiling:
            return cbr
    raise ValueError(
        f"no rung in MP3_CBR_LADDER={MP3_CBR_LADDER} fits under source_kbps="
        f"{source_kbps} (floor={ceiling}); the ladder floors at "
        f"{MP3_CBR_LADDER[-1]} kbps -- this is a policy gap, not a guess."
    )


def require_mp3_encoder(subtype: str = "MPEG_LAYER_III") -> None:
    """Mirror of stem_encode.require_mp3_encoder: refuse, never swap codec,
    when this soundfile cannot write MP3. The format list alone is not proof
    (a libsndfile can list MP3 and lack the Layer III encoder), so this writes
    a short real file in both modes ``encode_mp3`` uses, before the model
    loads (Codex on #6821). ``subtype`` is the layer ``encode_mp3`` writes;
    only a test asks for another."""
    import soundfile as sf

    if "MP3" not in sf.available_formats():
        raise RuntimeError(
            f"soundfile {sf.__version__} (libsndfile {sf.__libsndfile_version__}) "
            "cannot write MP3; stems for an MP3/M4A source need soundfile >= 0.13 "
            "with its bundled libsndfile >= 1.1"
        )
    _probe_mp3_write(subtype)


def _probe_mp3_write(subtype: str) -> None:
    """Mirror of stem_encode._probe_mp3_write: 50 ms of silence as ``subtype``
    at the CBR floor rung and at V0 VBR; any refusal is the error."""
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
                raise RuntimeError(
                    f"soundfile {sf.__version__} (libsndfile {sf.__libsndfile_version__}) "
                    f"lists MP3 but cannot write {subtype} ({mode} bitrate): {exc}"
                ) from exc
            if probe.stat().st_size == 0:
                raise RuntimeError(f"libsndfile wrote an empty {subtype} probe ({mode} bitrate)")


def sndfile_mp3_compression(cbr_kbps: int) -> float:
    """Mirror of stem_encode.sndfile_mp3_compression: libsndfile's 0..1
    compression level that lands LAME on ``cbr_kbps`` CBR, capped below 1.0."""
    top, bottom = MP3_MPEG1_MAX_KBPS, MP3_MPEG1_MIN_KBPS
    if not bottom <= cbr_kbps <= top:
        raise ValueError(f"cbr_kbps must be within {bottom}..{top}, got {cbr_kbps}")
    return min((top - cbr_kbps) / (top - bottom), SNDFILE_MP3_MAX_COMPRESSION)


def encode_mp3(arr: Any, sample_rate: int, out_path: Path, *, cbr_kbps: int | None) -> bytes:
    """Mirror of stem_encode.encode_mp3: LAME via soundfile to a REAL file
    (so libsndfile patches the Xing header), CBR at ``cbr_kbps`` or V0 VBR."""
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
        subtype="MPEG_LAYER_III",
        compression_level=level,
        bitrate_mode=mode,
    )
    return out_path.read_bytes()


def encode_mp3_part(
    arr: Any,
    sample_rate: int,
    *,
    mp3_settings: str,
    source_bytes: int,
    source_kbps: float,
    out_path: Path,
) -> tuple[bytes, str]:
    """Mirror of stem_encode.encode_part's MP3 branch, the ladder.

    Returns (bytes, the settings that produced them). Rung 1 is
    ``mp3_settings``; a part larger than the source file is re-encoded at the
    rung-2 CBR and, like the shared encoder, steps down ``MP3_CBR_LADDER`` one
    rate at a time until it fits (LAME padding can leave rung 2 a few hundred
    bytes over). Where some rate fits, the bytes and rate equal
    ``stem_encode.encode_part``'s. Only where even the lowest rate is over does
    the farm differ: it KEEPS that part and records it, where the local encoder
    raises, because the GPU separation is already paid for (size is a KPI, not
    a gate, for routes that encode elsewhere -- stem_encode.encode_part's
    docstring and stem_size_policy.py's module docstring).
    """
    cbr_kbps = MP3_CBR_KBPS if mp3_settings == "lame_320kbps_cbr" else None
    encoded = encode_mp3(arr, sample_rate, out_path, cbr_kbps=cbr_kbps)
    if len(encoded) <= source_bytes:
        return encoded, mp3_settings
    rung2_cbr = mp3_rung2_cbr_kbps(source_kbps)
    for cbr in MP3_CBR_LADDER[MP3_CBR_LADDER.index(rung2_cbr) :]:
        encoded = encode_mp3(arr, sample_rate, out_path, cbr_kbps=cbr)
        if len(encoded) <= source_bytes:
            break
    return encoded, f"lame_{cbr}kbps_cbr"


def stem_size_violations(
    *, source_bytes: int, part_sizes: dict[str, int], codec: str
) -> list[dict[str, Any]]:
    """Mirror of stem_size_policy.assert_lossy_stems_not_larger_than_source,
    as the dicts the manifest records (dataclasses.asdict of each violation)."""
    if codec in CONTROL_CODECS or codec not in LOSSY_SIZE_GATED:
        return []
    if source_bytes <= 0:
        raise ValueError(f"source_bytes must be positive, got {source_bytes}")
    return [
        {
            "name": name,
            "part_bytes": size,
            "source_bytes": source_bytes,
            "ratio": size / source_bytes,
        }
        for name, size in part_sizes.items()
        if size > source_bytes
    ]


def encode_stems(
    arrays: dict[str, Any],
    sample_rate: int,
    codec: str,
    *,
    source_bytes: int,
    source_kbps: float,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Every part in ``codec``, plus the manifest's ``size_policy`` block.

    ``codec`` is already resolved (resolve_stem_codec). The block has the same
    keys scripts/stem_bundle_worker.py writes, so a farm bundle and a local one
    read the same way. Opus keeps its old hard gate (a part over the source
    raises); MP3 follows the ladder and records any overage instead.
    """
    import tempfile

    stems: dict[str, bytes] = {}
    mp3_settings_by_part: dict[str, str] = {}
    if codec == "mp3":
        require_mp3_encoder()
        rung1 = mp3_lame_settings_for_source_kbps(source_kbps)
        with tempfile.TemporaryDirectory(prefix="farm-mp3-") as tmp:
            for part, arr in arrays.items():
                stems[part], mp3_settings_by_part[part] = encode_mp3_part(
                    arr,
                    sample_rate,
                    mp3_settings=rung1,
                    source_bytes=source_bytes,
                    source_kbps=source_kbps,
                    out_path=Path(tmp) / f"{part}.mp3",
                )
    else:
        for part, arr in arrays.items():
            stems[part] = encode_stem(arr, sample_rate, codec, source_kbps=source_kbps)
        if codec == "opus":
            assert_lossy_stems_not_larger_than_source(
                source_bytes=source_bytes,
                part_sizes={p: len(b) for p, b in stems.items()},
                codec=codec,
            )
    part_sizes = {p: len(b) for p, b in stems.items()}
    return stems, {
        "source_bytes": source_bytes,
        "source_kbps": round(source_kbps, 2),
        "mp3_settings": mp3_settings_by_part or None,
        "part_bytes": part_sizes,
        "violations": stem_size_violations(
            source_bytes=source_bytes, part_sizes=part_sizes, codec=codec
        ),
    }


def encode_stem(
    arr: Any,
    sample_rate: int,
    codec: str,
    *,
    source_kbps: float | None = None,
) -> bytes:
    """One [frames, channels] stem as bytes in ``codec``: FLAC direct, Opus via
    ffmpeg. MP3 never comes through here: it needs the size ladder, which is
    encode_stems's job.

    ffmpeg is already in the image (it is there for decode), so this adds no
    layer. Encoding happens IN THE CONTAINER, before the bytes ever leave, so
    the saving applies to the wire and not just to disk at the far end.

    When ``source_kbps`` is set, Opus bitrate is derived so four stems share
    roughly the source's total bitrate (mirrored stem_size_policy).
    """
    if codec == "flac":
        return encode_flac(arr, sample_rate)
    spec = STEM_CODECS.get(codec)
    if spec is None or spec["args"] is None:
        raise RuntimeError(
            f"stem codec {codec!r} has no single-part ffmpeg encode; known: "
            f"{', '.join(sorted(STEM_CODECS))}"
        )
    import subprocess

    encode_args = (
        opus_ffmpeg_audio_args(source_kbps)
        if codec == "opus" and source_kbps is not None
        else list(spec["args"])
    )

    wav = encode_flac(arr, sample_rate)  # lossless intermediate, in memory
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            *encode_args,
            *([] if "-f" in encode_args else ["-f", spec["ext"]]),
            "pipe:1",
        ],
        input=wav,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(
            f"ffmpeg {codec} encode failed rc={proc.returncode}: "
            f"{proc.stderr.decode(errors='replace')[-500:]}"
        )
    return proc.stdout


def stem_array(tensor: Any) -> Any:
    """One demucs [channels, frames] tensor as a float32 [frames, channels]
    array, soundfile's layout."""
    import numpy as np

    arr = tensor.detach().cpu().numpy().T.astype(np.float32, copy=False)
    if arr.ndim != 2 or arr.shape[1] not in (1, 2):
        raise RuntimeError(f"unexpected stem shape {arr.shape}")
    return arr


def encode_flac(arr: Any, sample_rate: int) -> bytes:
    """One [frames, channels] float32 stem as 16-bit FLAC bytes.

    PCM_16 matches what scripts/stem_bundle_worker.py already writes, so FLAC
    is a container/compression change at the same precision. Measured on the
    30 existing WAV bundles, FLAC lands at 42% of WAV (bass ~25%, drums ~50%).

    Not bit-identical to the WAV path, and it cannot be: libsndfile rounds
    float32 to int16 slightly differently per writer, so about half the
    samples differ by at most 1 LSB (-90 dBFS, under the 16-bit noise floor).
    Encoding int16 to FLAC IS lossless; only the float32 entry point rounds.
    """
    import io

    import soundfile as sf

    buffer = io.BytesIO()
    sf.write(buffer, arr, sample_rate, format="FLAC", subtype="PCM_16")
    return buffer.getvalue()


def audio_block(
    sample_rate: int, channels: int, frame_count: int, codec: str
) -> dict[str, Any]:
    """The manifest's ``audio`` block: alignment measured on the separated
    tensor (MP3 has no frame-exact header count) and the codec actually
    written. ``bit_depth`` exists only for FLAC, the one codec rendered at a
    fixed PCM depth (PCM_16); a lossy part has none, so none is claimed."""
    block: dict[str, Any] = {
        "sample_rate": sample_rate,
        "channels": channels,
        "frame_count": frame_count,
    }
    if codec == "flac":
        block["bit_depth"] = 16
    block["codec"] = codec
    return block


def assert_stem_size_policy_mirror_matches() -> None:
    """Keep mirrored Opus sizing identical to apps.stems.stem_size_policy."""
    from apps.stems import stem_size_policy as pol

    if (
        pol.STEM_PART_COUNT != STEM_PART_COUNT
        or pol.OPUS_MIN_KBPS != OPUS_MIN_KBPS
        or pol.OPUS_MAX_KBPS != OPUS_MAX_KBPS
        or pol.LOSSY_SIZE_GATED != LOSSY_SIZE_GATED
        or pol.CONTROL_CODECS != CONTROL_CODECS
    ):
        raise SystemExit(
            "error: stem_size_policy constants drifted from the Modal mirror"
        )
    for kbps in (96.0, 128.0, 256.0, 320.0, 512.0):
        if pol.opus_kbps_per_stem(kbps) != opus_kbps_per_stem(kbps):
            raise SystemExit(
                f"error: opus_kbps_per_stem({kbps}) drifted -- "
                f"policy={pol.opus_kbps_per_stem(kbps)} "
                f"farm={opus_kbps_per_stem(kbps)}"
            )
        if pol.opus_ffmpeg_audio_args(kbps) != opus_ffmpeg_audio_args(kbps):
            raise SystemExit(
                f"error: opus_ffmpeg_audio_args({kbps}) drifted from Modal mirror"
            )
    assert_mp3_ladder_mirror_matches()


def assert_mp3_ladder_mirror_matches() -> None:
    """Keep the mirrored MP3 ladder identical to its sources of truth,
    apps.stems.stem_size_policy (codec policy, rungs) and apps.stems.
    stem_encode (libsndfile's CBR mapping). Run before any GPU is bought; the
    byte-level equivalence of the encode itself is pinned by
    tests/stems/test_farm_mp3_ladder.py, which needs soundfile."""
    from apps.stems import stem_encode as enc
    from apps.stems import stem_size_policy as pol

    sources = (
        pol.LOSSLESS_SOURCE_EXTS, pol.LOSSY_SOURCE_EXTS, pol.MP3_CBR_KBPS, pol.MP3_CBR_LADDER,
        enc.MP3_MPEG1_MAX_KBPS, enc.MP3_MPEG1_MIN_KBPS, enc.SNDFILE_MP3_MAX_COMPRESSION,
    )
    mirror = (
        LOSSLESS_SOURCE_EXTS, LOSSY_SOURCE_EXTS, MP3_CBR_KBPS, MP3_CBR_LADDER,
        MP3_MPEG1_MAX_KBPS, MP3_MPEG1_MIN_KBPS, SNDFILE_MP3_MAX_COMPRESSION,
    )
    if sources != mirror:
        raise SystemExit("error: MP3 ladder constants drifted from the Modal mirror")
    _assert_mp3_ladder_functions_match(pol, enc)


def _assert_mp3_ladder_functions_match(pol: Any, enc: Any) -> None:
    """The mirrored policy functions answer exactly as their sources for every probed input."""
    for ext in sorted(LOSSLESS_SOURCE_EXTS | LOSSY_SOURCE_EXTS):
        for spelled in (ext, ext.upper()):
            if pol.stem_output_codec_for_source_ext(spelled) != (
                stem_output_codec_for_source_ext(spelled)
            ):
                raise SystemExit(f"error: source codec policy drifted for {spelled!r}")
    for kbps in (32.0, 47.9, 64.0, 127.9, 128.0, 159.9, 191.9, 192.0, 245.0, 256.0, 320.0, 321.5):
        if pol.mp3_lame_settings_for_source_kbps(kbps) != (
            mp3_lame_settings_for_source_kbps(kbps)
        ) or pol.mp3_rung2_cbr_kbps(kbps) != mp3_rung2_cbr_kbps(kbps):
            raise SystemExit(f"error: MP3 ladder rung drifted at {kbps} kbps")
    for cbr in MP3_CBR_LADDER:
        if enc.sndfile_mp3_compression(cbr) != sndfile_mp3_compression(cbr):
            raise SystemExit(f"error: libsndfile CBR mapping drifted at {cbr} kbps")
