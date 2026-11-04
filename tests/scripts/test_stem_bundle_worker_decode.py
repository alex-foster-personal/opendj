"""scripts/stem_bundle_worker.py in an install with no ffmpeg (STEM-50, STEM-57).

The packaged app ships no ffmpeg (packaged check of 316572f5, Fri 2 Oct 2026,
finding 3: every MP3 failed with "non-WAV input ... needs ffmpeg on PATH").

[if] no ffmpeg and odj-audio resolves [then] a non-WAV source passes the decoder preflight, [else stop].
[if] no ffmpeg and no odj-audio [then] the preflight refuses naming both, before the model loads, [else stop].
[if] ffmpeg resolves [then] nothing changes: the policy codec and ffmpeg decode stand, [else stop].
[if] the policy codec is mp3 and no ffmpeg [then] parts are still MP3, encoded by soundfile's own
  LAME, and no part is larger than the source, [else stop]. STEM-51's lossless FLAC fallback made
  every part of an MP3 source 2.5x to 4.2x the source file (measured on a real 60 s mix).
[if] soundfile cannot write MP3 [then] the worker refuses before the model loads, never a codec swap.

These are the worker's pure decisions, run without torch (it never enters the
repo venv). The decode itself is ``odj-audio decode``, exercised for real in
tests/shared/test_odj_audio_decode.py.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

import scripts.stem_bundle_worker as worker
from apps.shared.odj_audio_decode import NoDecoderError
from apps.stems import stem_size_policy as pol

pytestmark = pytest.mark.requirement("STEM-50")

MP3 = Path("/music/track.mp3")


@pytest.fixture
def no_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PATH holding no ffmpeg and no MDT_FFMPEG, as in the installed app."""
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("PATH", str(bare))
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    assert shutil.which("ffmpeg") is None


def _engine(tmp_path: Path) -> Path:
    engine = tmp_path / "payload" / "bin" / "odj-audio"
    engine.parent.mkdir(parents=True)
    shutil.copy2(sys.executable, engine)
    engine.chmod(0o755)
    return engine


def test_no_ffmpeg_with_odj_audio_passes_the_decoder_preflight(
    no_ffmpeg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(_engine(tmp_path)))

    assert worker._ffmpeg_reachable() is False
    worker._preflight_decoder(MP3)


def test_no_ffmpeg_and_no_odj_audio_refuses_naming_both(
    no_ffmpeg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(tmp_path / "no-such-odj-audio"))

    with pytest.raises(NoDecoderError) as raised:
        worker._preflight_decoder(MP3)

    message = str(raised.value)
    for named in (str(MP3), "ffmpeg", "MDT_FFMPEG", "ODJ_AUDIO_BIN", "pre-transcode"):
        assert named in message, named
    # Control: a WAV needs neither, so the same environment lets it through.
    worker._preflight_decoder(Path("/music/track.wav"))


@pytest.mark.requirement("STEM-57")
@pytest.mark.requires_audio_stack
def test_mp3_source_keeps_the_mp3_policy_codec_without_ffmpeg(no_ffmpeg: None) -> None:
    assert worker._ffmpeg_reachable() is False
    assert worker._output_codec(MP3) == "mp3"
    assert worker._output_codec(Path("/music/track.m4a")) == "mp3"
    # STEM-8231: a lossless source gets MP3 parts too (four FLAC parts cannot
    # fit inside one source).
    assert worker._output_codec(Path("/music/track.flac")) == "mp3"


@pytest.mark.requirement("STEM-57")
@pytest.mark.requires_audio_stack
def test_mp3_encoder_capability_comes_from_the_real_libsndfile(no_ffmpeg: None) -> None:
    """The MP3 decision reads this host's real libsndfile: where it can write
    MP3 an MP3 source keeps the MP3 codec, and where it cannot the worker
    refuses before any model loads. Only the branch this host's libsndfile
    takes is measured here; the other is not simulated."""
    import soundfile as sf
    # STEM-8231: a lossless source is MP3 too, so it takes the same branch.
    for source in (MP3, Path("/music/track.wav")):
        if "MP3" in sf.available_formats():
            assert worker._output_codec(source) == "mp3"
        else:
            with pytest.raises(RuntimeError, match="cannot write MP3"):
                worker._output_codec(source)


@pytest.mark.requirement("STEM-57")
@pytest.mark.requires_audio_stack
def test_the_mp3_preflight_refuses_a_listed_layer_this_libsndfile_cannot_write() -> None:
    """[if] libsndfile lists an MP3 layer it cannot encode [then] the preflight refuses it by name before any model loads, [else stop].

    MPEG Layer II is listed under MP3 by libsndfile 1.2 yet is decode-only,
    the same shape as an install whose Layer III encoder is missing (Codex on
    #6820): the format list passes, only a real write fails. Real libsndfile,
    real write; no encoder is simulated.

    MUTATION TARGET: drop ``_probe_mp3_write`` from ``require_mp3_encoder``
    (or make it a format-list check) and the Layer II probe passes.
    """
    import soundfile as sf

    from apps.stems import stem_encode

    if "MPEG_LAYER_II" not in sf.available_subtypes("MP3"):
        pytest.skip(f"UNAVAILABLE: libsndfile {sf.__libsndfile_version__} does not list MPEG Layer II")
    with pytest.raises(stem_encode.Mp3EncoderUnavailableError, match="cannot write MPEG_LAYER_II"):
        stem_encode.require_mp3_encoder("MPEG_LAYER_II")
    # Control: the layer the worker writes passes the same preflight.
    stem_encode.require_mp3_encoder()


def test_with_ffmpeg_nothing_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MDT_FFMPEG alone (the worker trusts it as given) keeps the dev path."""
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("PATH", str(bare))
    monkeypatch.setenv("MDT_FFMPEG", "/opt/ffmpeg/bin/ffmpeg")
    monkeypatch.setenv("ODJ_AUDIO_BIN", str(tmp_path / "no-such-odj-audio"))

    assert worker._ffmpeg_reachable() is True
    # odj-audio is not consulted at all when ffmpeg resolves.
    worker._preflight_decoder(MP3)


def _noise(np: Any, seconds: float, rate: int = 44100) -> Any:
    """Stereo white noise at -6 dBFS: the hardest input for LAME, so a size
    bound that holds here holds for any separated stem."""
    rng = np.random.default_rng(57)
    return (0.5 * rng.uniform(-1.0, 1.0, size=(2, int(seconds * rate)))).astype(np.float32)


@pytest.mark.requirement("STEM-57")
@pytest.mark.requires_audio_stack
@pytest.mark.parametrize(
    ("source_kbps", "settings"), [(320.0, "lame_80kbps_cbr"), (256.0, "lame_64kbps_cbr"), (192.0, "lame_64kbps_cbr")]
)
def test_mp3_parts_encode_with_no_ffmpeg_and_never_exceed_the_source(
    no_ffmpeg: None, tmp_path: Path, source_kbps: float, settings: str
) -> None:
    """The real encode chain, with no ffmpeg anywhere, writes an MP3 part no
    larger than a QUARTER of a real CBR MP3 source of the same length (STEM-8231:
    the four parts together fit the source), and it decodes to at
    least the frames it was given (LAME adds at most two frames of padding)."""
    import numpy as np
    import soundfile as sf
    rate, seconds = 44100, 4.0
    noise = _noise(np, seconds, rate)
    source = worker._encode_mp3(noise.T, rate, tmp_path / "source.mp3", cbr_kbps=int(source_kbps))
    source_bytes = len(source)
    out = tmp_path / "vocals.mp3"

    encoded, _rung2 = worker._encode_stem_part(
        noise,
        rate,
        codec=worker._output_codec(MP3),
        part="vocals",
        mp3_settings=settings,
        source_bytes=source_bytes,
        source_kbps=source_bytes * 8 / seconds / 1000,
        out_path=out,
    )

    assert out.read_bytes() == encoded
    if source_kbps >= 256:
        assert 4 * len(encoded) <= source_bytes, f"{len(encoded)}B part over a quarter of a {source_bytes}B source"
    else:  # the 64 kbps floor wins over the size rule
        assert len(encoded) <= source_bytes, f"{len(encoded)}B floor part over a {source_bytes}B source"
    info = sf.info(str(out))
    assert (info.format, info.samplerate, info.channels) == ("MP3", rate, 2)
    # Count DECODED frames: a low-rate CBR part carries no Info header, so
    # sf.info's frame count is a byte-size estimate (measured +3001 frames at
    # 48 kbps on 4 s) while the real decode is within LAME's two-frame pad.
    decoded_frames = len(sf.read(str(out), dtype="float32")[0])
    assert int(seconds * rate) <= decoded_frames <= int(seconds * rate) + 2 * 1152


@pytest.mark.requirement("STEM-57")
@pytest.mark.requires_audio_stack
@pytest.mark.parametrize("cbr_kbps", pol.MP3_CBR_LADDER)
def test_each_cbr_rung_encodes_at_its_own_bitrate(no_ffmpeg: None, tmp_path: Path, cbr_kbps: int) -> None:
    """libsndfile's compression level lands exactly on every ladder rate, so
    rung 2 (nearest CBR <= the source) really is under the source. The rates
    come from the production ladder, so a rung added there is tested here."""
    import numpy as np
    rate, seconds = 44100, 8.0
    out = tmp_path / f"part-{cbr_kbps}.mp3"

    encoded = worker._encode_mp3(_noise(np, seconds, rate).T, rate, out, cbr_kbps=cbr_kbps)

    # The first frame header's bitrate index (MPEG-1 Layer III table) names
    # the rate LAME was set to; the byte rate is the measured cross-check.
    mpeg1_l3_kbps = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    assert encoded[:2] == b"\xff\xfb", encoded[:4]
    assert mpeg1_l3_kbps[encoded[2] >> 4] == cbr_kbps
    measured_kbps = len(encoded) * 8 / seconds / 1000
    assert abs(measured_kbps - cbr_kbps) < 0.02 * cbr_kbps, measured_kbps


@pytest.mark.requirement("STEM-57")
@pytest.mark.requires_audio_stack
def test_v0_vbr_part_carries_a_xing_header(no_ffmpeg: None, tmp_path: Path) -> None:
    """A VBR mp3 without its Xing header reads the wrong duration in every
    fast reader; libsndfile writes it when the output is a real file."""
    import numpy as np
    out = tmp_path / "other.mp3"

    encoded = worker._encode_mp3(_noise(np, 2.0).T, 44100, out, cbr_kbps=None)

    assert b"Xing" in encoded[:4096]


@pytest.mark.requirement("STEM-57")
def test_the_ladder_test_covers_the_whole_mpeg1_layer3_table() -> None:
    """Control for the parametrization above: the ladder it reads is the full
    standard table, so "every rung" cannot quietly become "the rungs left"."""
    assert sorted(pol.MP3_CBR_LADDER) == [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]


def _encode_against(
    tmp_path: Path, np: Any, *, source_bytes: int, source_kbps: float, seconds: float = 3.0, part: str = "vocals"
) -> tuple[Path, bytes, str | None]:
    out = tmp_path / "drums.mp3"
    encoded, settings = worker._encode_stem_part(
        _noise(np, seconds),
        44100,
        codec="mp3",
        part=part,
        mp3_settings=pol.mp3_lame_settings_for_source_kbps(source_kbps),
        source_bytes=source_bytes,
        source_kbps=source_kbps,
        out_path=out,
    )
    return out, encoded, settings


@pytest.mark.requirement("STEM-57")
@pytest.mark.requires_audio_stack
def test_a_part_still_over_the_source_at_rung_2_steps_down_until_it_fits(no_ffmpeg: None, tmp_path: Path) -> None:
    """A source a hair above 512 kbps (rung 1: 128 per part) whose QUARTER
    sits between a 128 and a 112 kbps part: 128 is over the part's budget, so
    the part is written at 112 (the STEM-8231 safety rung)."""
    import numpy as np
    seconds = 3.0
    at_128 = len(worker._encode_mp3(_noise(np, seconds).T, 44100, tmp_path / "at128.mp3", cbr_kbps=128))
    at_112 = len(worker._encode_mp3(_noise(np, seconds).T, 44100, tmp_path / "at112.mp3", cbr_kbps=112))
    source_bytes = 2 * (at_128 + at_112)
    budget = pol.whole_set_part_budget_bytes(source_bytes)
    assert at_112 < budget < at_128, "control: the part budget really sits between the two rungs"

    out, encoded, settings = _encode_against(tmp_path, np, source_bytes=source_bytes, source_kbps=512.04)

    assert settings == "lame_112kbps_cbr"
    assert len(encoded) == at_112 <= budget
    assert out.read_bytes() == encoded


@pytest.mark.requirement("STEM-57")
@pytest.mark.requires_audio_stack
def test_a_part_over_its_quarter_at_the_floor_is_kept_at_64_kbps(no_ffmpeg: None, tmp_path: Path) -> None:
    """[if] the safety rung steps under 64 kbps or drops the part [then] broken:
    the maintainer chose the 64 kbps floor over the size rule, so a part still over its
    quarter at 64 is written at 64."""
    import numpy as np
    seconds = 3.0
    at_floor = len(worker._encode_mp3(_noise(np, seconds).T, 44100, tmp_path / "floor.mp3", cbr_kbps=64))
    source_bytes = 4 * at_floor - 4  # each part's budget is one byte short of the floor part

    out, encoded, settings = _encode_against(tmp_path, np, source_bytes=source_bytes, source_kbps=320.0)

    assert (len(encoded), settings) == (at_floor, "lame_64kbps_cbr")
    assert out.read_bytes() == encoded


@pytest.mark.requirement("STEM-8231")
@pytest.mark.requires_audio_stack
@pytest.mark.parametrize(("part", "source_kbps", "want"), [
    ("bass", 320.0, "lame_96kbps_cbr"),  # rung 1 is 80: lifted to the 96 kbps floor
    ("drums", 192.0, "lame_96kbps_cbr"),  # rung 1 is the 64 floor: lifted to 96
    ("drums", 896.0, None),  # 224 is above 96: rung 1 holds
    ("vocals", 320.0, None),  # vocals keep 80
    ("other", 192.0, None),  # other keeps the 64 floor
])
def test_bass_and_drums_never_go_under_96_kbps(
    no_ffmpeg: None, tmp_path: Path, part: str, source_kbps: float, want: str | None
) -> None:
    """[if] a bass or drums part is encoded under 96 kbps [then] broken: the maintainer
    picked 96 by ear ("96k is the one", Tue 3 Nov 2026)."""
    import numpy as np
    _out, encoded, settings = _encode_against(
        tmp_path, np, source_bytes=10**9, source_kbps=source_kbps, part=part
    )
    assert settings == want
    mpeg1_l3_kbps = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    assert mpeg1_l3_kbps[encoded[2] >> 4] >= pol.mp3_part_floor_kbps(part)


@pytest.mark.requirement("STEM-8231")
@pytest.mark.requires_audio_stack
@pytest.mark.parametrize("cbr_kbps", [64, 80, 128, 320])
def test_parts_at_the_floor_and_up_decode_with_zero_offset(no_ffmpeg: None, tmp_path: Path, cbr_kbps: int) -> None:
    """[if] a part at 64 kbps or more decodes shifted against its input [then]
    broken: the Info header that lets decoders trim LAME's delay is missing."""
    import numpy as np
    import soundfile as sf
    rate = 44100
    noise = _noise(np, 6.0, rate).T
    out = tmp_path / f"part-{cbr_kbps}.mp3"
    encoded = worker._encode_mp3(noise, rate, out, cbr_kbps=cbr_kbps)
    assert b"Info" in encoded[:4096] or b"Xing" in encoded[:4096]
    decoded = sf.read(str(out), dtype="float32")[0]
    window = slice(2000, 40000)
    scores = {k: float(np.dot(noise[window, 0], decoded[2000 + k : 40000 + k, 0])) for k in range(0, 1200)}
    assert max(scores, key=scores.get) == 0
