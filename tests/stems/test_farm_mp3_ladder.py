"""The Modal farm writes MP3 stems through the STEM-57 ladder, in a v3 bundle (STEM-75).

[if] the farm writes an MP3 source's stems as anything but ladder MP3 in a v3 bundle [then] fail, [else stop].

Until STEM-75 every farm route (the relay backend and the direct Modal worker)
asked for FLAC whatever the source, so an MP3 source came home as four
lossless parts several times the size of the file they were separated from.
The farm runs inside a Modal container with no ``apps`` package, so it keeps
MIRRORS of ``apps.stems.stem_size_policy`` and ``apps.stems.stem_encode`` in
``scripts/farm_stem_encode.py``. This file pins those mirrors by BEHAVIOR,
not by text: it runs them on real audio through the real soundfile/LAME, next
to the real ``apps.stems.stem_encode``. The farm's own manifest and bundle
writer are lifted out of its source (no modal import, the same approach as
``test_bundle_publish_mirror.py``).

  - [if] the source is MP3/M4A and the caller asked ``by-source`` [then] every
    part is MP3 [⛔️ if any part is FLAC]
  - [if] a caller names ``flac`` [then] every part is FLAC, MP3 source or not
  - [if] the farm encodes a part [then] its bytes and rung equal
    ``apps.stems.stem_encode.encode_part``'s for the same input [⛔️ on drift]
  - [if] the source is under 192 kbps (a 128 kbps rip) [then] rung 2 is the
    nearest MPEG-1 rate at or under it [⛔️ if it raises after the GPU work]
  - [if] a rung-2 part is still over the source [then] the farm steps down
    the CBR ladder like ``encode_part`` until it fits [⛔️ if it publishes the
    oversized rung-2 part while a lower rate fits]
  - [if] even the lowest rate is over the source [then] it is kept and recorded
    in ``size_policy.violations`` [⛔️ if it raises and discards the separation]
  - [if] the bundle is written [then] the real stem reader loads it as v3 MP3
  - [if] an extension has no policy [then ⛔️] a per-track error, before GPU

-Claude
"""

from __future__ import annotations

import ast
import importlib.util
import json
import time
from pathlib import Path
from typing import Any

import pytest

from apps.stems import bundle_publish

try:
    import numpy as np
    import soundfile as sf
except ImportError:
    # Never a module-level skip: collection must reach the requires_audio_stack
    # marker below, which tests/conftest.py turns into an UNAVAILABLE capability
    # report per test (Codex on #6821). Every use of np/sf is inside a test.
    np = sf = None  # type: ignore[assignment]

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
FARM = SCRIPTS / "modal_vocal_farm.py"
ENCODER = SCRIPTS / "farm_stem_encode.py"
RATE = 44100
SECONDS = 3.0
PARTS = bundle_publish.STEM_PARTS

# Lifted from the farm itself: the bundle writer stays in modal_vocal_farm.py.
FARM_FUNCTIONS = ("_stem_manifest", "_publish_stems_local")
FARM_CONSTANTS = ("DEMUCS_VERSION", "STEM_PARTS", "STEM_BUNDLE_SCHEMA", "STEM_LAYOUT")

pytestmark = [pytest.mark.requirement("STEM-75"), pytest.mark.requires_audio_stack]


def _assigned_name(node: ast.stmt) -> str | None:
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return None


def _farm() -> dict[str, Any]:
    """A FRESH copy of scripts/farm_stem_encode.py (so a test that drifts a
    constant cannot leak into the next), plus the farm's own bundle writer
    executed from its source into the same namespace."""
    spec = importlib.util.spec_from_file_location("farm_stem_encode_under_test", ENCODER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    namespace = vars(module)
    namespace.update({"json": json, "time": time})
    wanted = set(FARM_CONSTANTS) | set(FARM_FUNCTIONS)
    found: set[str] = set()
    for node in ast.parse(FARM.read_text(encoding="utf-8")).body:
        name = node.name if isinstance(node, ast.FunctionDef) else _assigned_name(node)
        if name in wanted:
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(FARM), "exec"), namespace)
            found.add(name)
    missing = wanted - found
    assert not missing, f"not found in {FARM}: {sorted(missing)}"
    return namespace


def _noise(seconds: float = SECONDS, gain: float = 0.5, seed: int = 75) -> Any:
    """[frames, channels] stereo white noise: LAME's hardest input."""
    rng = np.random.default_rng(seed)
    return (gain * rng.uniform(-1.0, 1.0, size=(int(seconds * RATE), 2))).astype(np.float32)


def _arrays() -> dict[str, Any]:
    """Four different parts, from full-scale noise down to a soft sine, so
    one batch exercises rung 1 and rung 2."""
    gains = {"vocals": 0.5, "drums": 0.25, "bass": 0.05}
    arrays = {part: _noise(gain=gains[part], seed=i) for i, part in enumerate(PARTS[:3])}
    t = np.arange(int(SECONDS * RATE), dtype=np.float32) / RATE
    tone = (0.1 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    arrays["other"] = np.stack([tone, tone], axis=1)
    return arrays


def _cbr_source(tmp_path: Path, kbps: int) -> tuple[int, float]:
    """A real CBR MP3 'source' of the same length; (bytes, effective kbps)."""
    from apps.stems.stem_encode import encode_mp3

    data = encode_mp3(_noise(), RATE, tmp_path / f"source-{kbps}.mp3", cbr_kbps=kbps)
    return len(data), len(data) * 8 / SECONDS / 1000


def test_the_mirror_check_passes_against_the_real_modules() -> None:
    _farm()["assert_stem_size_policy_mirror_matches"]()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MP3_CBR_LADDER", (320, 256, 224, 192)),
        ("MP3_CBR_KBPS", 256),
        ("LOSSY_SOURCE_EXTS", frozenset({".mp3"})),
        ("MP3_MPEG1_MIN_KBPS", 8),
    ],
)
def test_negative_control_a_drifted_mirror_stops_the_run(name: str, value: Any) -> None:
    farm = _farm()
    farm[name] = value
    with pytest.raises(SystemExit):
        farm["assert_mp3_ladder_mirror_matches"]()


@pytest.mark.parametrize(
    ("asked", "source", "codec"),
    [
        ("by-source", "a.mp3", "mp3"),
        ("by-source", "a.M4A", "mp3"),
        ("by-source", "a.flac", "flac"),
        ("by-source", "a.aiff", "flac"),
        ("flac", "a.mp3", "flac"),
        ("mp3", "a.wav", "mp3"),
    ],
)
def test_the_codec_follows_the_source_unless_the_caller_names_one(
    asked: str, source: str, codec: str
) -> None:
    assert _farm()["resolve_stem_codec"](asked, source) == codec


def test_the_farm_loads_this_encoder_and_ships_it_in_its_image() -> None:
    """The farm module reads farm_stem_encode.py as its sibling and its image
    copies that file to /root, where the sibling is when Modal runs the farm
    as a single file."""
    source = FARM.read_text(encoding="utf-8")
    assert 'Path(__file__).with_name("farm_stem_encode.py")' in source
    assert '_FARM_STEM_ENCODE_REMOTE = "/root/farm_stem_encode.py"' in source
    assert ".add_local_file(_FARM_STEM_ENCODE, _FARM_STEM_ENCODE_REMOTE, copy=True)" in source


def test_the_default_is_the_source_policy() -> None:
    tree = ast.parse(FARM.read_text(encoding="utf-8"))
    default = next(
        node.value
        for node in tree.body
        if _assigned_name(node) == "DEFAULT_STEM_CODEC" and isinstance(node, ast.AnnAssign)
    )
    assert isinstance(default, ast.Name) and default.id == "STEM_CODEC_BY_SOURCE"
    assert _farm()["STEM_CODEC_BY_SOURCE"] == bundle_publish.STEM_CODEC_BY_SOURCE


@pytest.mark.parametrize(("asked", "source"), [("by-source", "a.ogg"), ("wav", "a.mp3")])
def test_no_policy_or_unknown_codec_is_refused(asked: str, source: str) -> None:
    with pytest.raises((ValueError, RuntimeError)):
        _farm()["resolve_stem_codec"](asked, source)


@pytest.mark.parametrize("source_kbps", [192, 256, 320])
def test_farm_mp3_parts_equal_the_shared_encoder_and_fit_the_source(
    tmp_path: Path, source_kbps: int
) -> None:
    from apps.stems import stem_size_policy as pol
    from apps.stems.stem_encode import encode_part

    farm = _farm()
    source_bytes, effective_kbps = _cbr_source(tmp_path, source_kbps)
    arrays = _arrays()

    stems, size_policy = farm["encode_stems"](
        arrays, RATE, "mp3", source_bytes=source_bytes, source_kbps=effective_kbps
    )

    rung1 = pol.mp3_lame_settings_for_source_kbps(effective_kbps)
    settings = size_policy["mp3_settings"]
    for part in PARTS:
        expected, rung2 = encode_part(
            arrays[part],
            RATE,
            codec="mp3",
            mp3_settings=rung1,
            source_bytes=source_bytes,
            source_kbps=effective_kbps,
            out_path=tmp_path / f"shared-{part}.mp3",
        )
        assert stems[part] == expected, f"{part}: farm bytes differ from stem_encode"
        assert settings[part] == (rung2 or rung1), part
        assert stems[part][:3] == b"ID3" or stems[part][:2] in (b"\xff\xfb", b"\xff\xfa")
        assert len(stems[part]) <= source_bytes, f"{part} outgrew its source"
    assert size_policy["violations"] == []
    assert size_policy["part_bytes"] == {p: len(b) for p, b in stems.items()}
    assert set(size_policy) == {
        "source_bytes",
        "source_kbps",
        "mp3_settings",
        "part_bytes",
        "violations",
    }


def test_rung_two_actually_fires_on_a_loud_part(tmp_path: Path) -> None:
    """Positive control for the ladder: against a 192 kbps source, full-scale
    noise at V0 is larger than the source, so the farm must re-encode it at
    192 CBR; the near-silent part must stay on rung 1."""
    farm = _farm()
    source_bytes, effective_kbps = _cbr_source(tmp_path, 192)
    stems, size_policy = farm["encode_stems"](
        _arrays(), RATE, "mp3", source_bytes=source_bytes, source_kbps=effective_kbps
    )
    assert size_policy["mp3_settings"]["vocals"] == "lame_192kbps_cbr"
    assert size_policy["mp3_settings"]["other"] == "lame_v0_vbr"
    mpeg1_l3_kbps = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    assert mpeg1_l3_kbps[stems["vocals"][2] >> 4] == 192


def test_a_rung_two_part_over_by_a_hair_steps_down_like_the_shared_encoder(tmp_path: Path) -> None:
    """Codex P2 on #6821: when the rung-2 CBR lands just over the source, the
    farm keeps stepping down ``MP3_CBR_LADDER`` exactly as
    ``stem_encode.encode_part`` does, so the published part fits and its bytes
    and rate equal the shared encoder's."""
    from apps.stems.stem_encode import encode_part

    farm = _farm()
    noise = _noise()
    at_192 = farm["encode_mp3"](noise, RATE, tmp_path / "probe.mp3", cbr_kbps=192)
    source_bytes = len(at_192) - 1  # rung 2 (192) is over by one byte

    stems, size_policy = farm["encode_stems"](
        {"vocals": noise}, RATE, "mp3", source_bytes=source_bytes, source_kbps=192.0
    )

    expected, settings = encode_part(
        noise,
        RATE,
        codec="mp3",
        mp3_settings=farm["mp3_lame_settings_for_source_kbps"](192.0),
        source_bytes=source_bytes,
        source_kbps=192.0,
        out_path=tmp_path / "shared.mp3",
    )
    assert settings == "lame_160kbps_cbr"
    assert size_policy["mp3_settings"]["vocals"] == settings
    assert stems["vocals"] == expected
    assert len(stems["vocals"]) <= source_bytes
    assert size_policy["violations"] == []


def test_a_part_still_over_the_source_is_kept_and_recorded(tmp_path: Path) -> None:
    """Size is a KPI, not a gate: a separation is never thrown away for it.
    Overshoot control for the step-down above: past the 32 kbps floor the farm
    keeps the floor encode and records it rather than raising."""
    farm = _farm()
    stems, size_policy = farm["encode_stems"](
        {"vocals": _noise()}, RATE, "mp3", source_bytes=1000, source_kbps=200.0
    )
    assert len(stems["vocals"]) > 1000
    assert size_policy["mp3_settings"]["vocals"] == "lame_32kbps_cbr"
    assert [v["name"] for v in size_policy["violations"]] == ["vocals"]
    assert size_policy["violations"][0]["ratio"] > 1.0


def test_an_explicit_flac_request_stays_lossless(tmp_path: Path) -> None:
    farm = _farm()
    source_bytes, effective_kbps = _cbr_source(tmp_path, 320)
    stems, size_policy = farm["encode_stems"](
        _arrays(), RATE, "flac", source_bytes=source_bytes, source_kbps=effective_kbps
    )
    assert all(blob[:4] == b"fLaC" for blob in stems.values())
    assert size_policy["mp3_settings"] is None
    assert size_policy["violations"] == []


@pytest.mark.parametrize("codec", ["mp3", "flac"])
def test_the_audio_block_claims_a_bit_depth_only_for_flac(codec: str) -> None:
    block = _farm()["audio_block"](RATE, 2, 1000, codec)
    assert block["codec"] == codec
    assert ("bit_depth" in block) == (codec == "flac")


def test_a_farm_mp3_bundle_loads_through_the_real_reader(tmp_path: Path) -> None:
    """End to end on the farm's own code: encode, manifest, publish, then the
    deck's reader (apps.stems.artifacts) loads it as v3 MP3."""
    from apps.stems.artifacts import load_stem_bundle

    farm = _farm()
    source_bytes, effective_kbps = _cbr_source(tmp_path, 256)
    arrays = _arrays()
    stems, size_policy = farm["encode_stems"](
        arrays, RATE, "mp3", source_bytes=source_bytes, source_kbps=effective_kbps
    )
    frames = int(SECONDS * RATE)
    result = {
        "stems": stems,
        "size_policy": size_policy,
        "stem_codec": "mp3",
        "stem_ext": farm["STEM_CODECS"]["mp3"]["ext"],
        "source_sha256": "a" * 64,
        "audio": farm["audio_block"](RATE, 2, frames, "mp3"),
    }
    preset = bundle_publish.PRESETS["hdemucs_mmi-ov0.25"]
    sid = "b" * 40
    farm["_publish_stems_local"](sid, Path("/music/a.mp3"), result, preset, tmp_path)

    bundle = load_stem_bundle(sid, stems_dir=tmp_path / "state" / "stems")
    assert bundle.media_type == "audio/mpeg"
    assert bundle.alignment.frame_count == frames
    assert {path.suffix for path in bundle.files.values()} == {".mp3"}
    manifest = json.loads((tmp_path / "state" / "stems" / sid / "manifest.json").read_text())
    assert manifest["schema_version"] == 3
    assert manifest["codec"] == "mp3"
    for part, path in bundle.files.items():
        info = sf.info(str(path))
        assert info.format == "MP3", part
        assert frames <= info.frames <= frames + 2 * 1152, part


@pytest.mark.parametrize(("source_kbps", "rung2"), [(128, 128), (160, 160), (96, 96)])
def test_a_sub_192_source_reencodes_at_its_own_rate_instead_of_raising(
    tmp_path: Path, source_kbps: int, rung2: int
) -> None:
    """Sentry on #6203 (HIGH): the old ladder floored at 192, so a 128 kbps
    source raised at rung 2 after the GPU time was spent. Full-scale noise at
    V0 is larger than such a source, so rung 2 must fire, at the source's own
    MPEG-1 rate, and the part must fit."""
    farm = _farm()
    source_bytes, effective_kbps = _cbr_source(tmp_path, source_kbps)
    stems, size_policy = farm["encode_stems"](
        {"vocals": _noise()}, RATE, "mp3", source_bytes=source_bytes, source_kbps=effective_kbps
    )
    assert size_policy["mp3_settings"]["vocals"] == f"lame_{rung2}kbps_cbr"
    mpeg1_l3_kbps = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    assert mpeg1_l3_kbps[stems["vocals"][2] >> 4] == rung2
    assert len(stems["vocals"]) <= source_bytes


def test_below_the_mpeg1_floor_there_is_no_rung() -> None:
    """Control for the overshoot: the ladder still has a floor (32 kbps)."""
    with pytest.raises(ValueError, match="no rung"):
        _farm()["mp3_rung2_cbr_kbps"](31.9)


def test_the_farm_preflight_refuses_a_listed_layer_this_libsndfile_cannot_write() -> None:
    """[if] libsndfile lists MP3 but cannot write the layer [then] the farm preflight raises, [else stop].

    The format list passes for any MP3 layer; libsndfile lists MPEG_LAYER_II
    and cannot write it, so that layer is a real negative for the write probe
    the farm runs before Demucs loads (Codex on #6821). Layer III, the one
    ``encode_mp3`` writes, is the control."""
    farm = _farm()
    assert "MP3" in sf.available_formats()
    with pytest.raises(RuntimeError, match="cannot write MPEG_LAYER_II"):
        farm["require_mp3_encoder"]("MPEG_LAYER_II")
    farm["require_mp3_encoder"]()
