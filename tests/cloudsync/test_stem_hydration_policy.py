"""A downloaded stem-set follows the STEM-8231 size rule on arrival (#9665).

The in-memory bucket (``fake_s3``) stands in for R2; parts are real FLAC and
MP3 audio and the source is a real MP3, so the hydrate's own classify,
re-encode and strict load run.

[if] a FLAC stem-set of an MP3 source lands as FLAC [then] hydrate re-encodes or refuses by name, [else stop].
[if] a no-encoder hydrate of a rule-breaking set leaves a directory or no named reason [then] broken, [else stop].
[if] a compliant MP3 stem-set's bytes change on hydrate [then] broken, [else stop].
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

try:
    import soundfile as sf
except ImportError:
    sf = None  # type: ignore[assignment]

from apps.cloud.asset_store import asset_object_key
from apps.cloud.config import CloudConfig
from apps.cloud.stem_hydrate_policy import (
    HYDRATE_REENCODED,
    HYDRATE_REFUSED_NO_MP3_ENCODER,
    STEM_HYDRATE_POLICY_REFUSED,
)
from apps.cloud.stem_hydration import hydrate_one
from apps.cloud.stem_source import DirectR2Source
from apps.stems.artifacts import StemBundleNotFoundError, load_stem_bundle
from apps.stems.stem_encode import encode_mp3
from tests.stems.recompress_support import FRAMES, PARTS, RATE, _noise, _source

pytestmark = [pytest.mark.requirement("STEM-8231"), pytest.mark.requires_audio_stack]

AT_RULE_192 = {"vocals": 64, "other": 64, "drums": 96, "bass": 96}
SID = "remote-track"


def _manifest(source: Path, ext: str, *, schema: int, codec: str | None) -> bytes:
    manifest: dict[str, object] = {
        "schema_version": schema,
        "stable_id": SID,
        "model": {"name": "hdemucs_mmi", "version": "4.0.1"},
        "source": {"path": str(source), "sha256": "0" * 64},
        "files": {part: f"{part}.{ext}" for part in PARTS},
        "preset": {"tag": "hdemucs_mmi-ov0.25"},
        "audio": {"sample_rate": RATE, "frame_count": FRAMES, "channels": 2},
    }
    if codec is not None:
        manifest |= {"layout": "demucs4", "codec": codec}
    return json.dumps(manifest).encode()


def _flac_set(scratch: Path, source: Path) -> dict[str, bytes]:
    """The legacy farm schema v2 shape: FLAC parts, no codec key."""
    scratch.mkdir(exist_ok=True)
    files = {"manifest.json": _manifest(source, "flac", schema=2, codec=None)}
    for i, part in enumerate(PARTS):
        sf.write(scratch / f"{part}.flac", _noise(i), RATE, format="FLAC", subtype="PCM_16")
        files[f"{part}.flac"] = (scratch / f"{part}.flac").read_bytes()
    return files


def _mp3_set(scratch: Path, source: Path) -> dict[str, bytes]:
    """A compliant schema v3 MP3 set for a 192 kbps source."""
    scratch.mkdir(exist_ok=True)
    files = {"manifest.json": _manifest(source, "mp3", schema=3, codec="mp3")}
    for i, part in enumerate(PARTS):
        encode_mp3(_noise(i), RATE, scratch / f"{part}.mp3", cbr_kbps=AT_RULE_192[part])
        files[f"{part}.mp3"] = (scratch / f"{part}.mp3").read_bytes()
    return files


def _seed(fake_s3, cfg: CloudConfig, files: dict[str, bytes]) -> dict[str, str]:
    entry: dict[str, str] = {}
    for name, body in files.items():
        digest = hashlib.sha256(body).hexdigest()
        fake_s3.put_object_if_none_match(cfg.audio_bucket, asset_object_key(digest), body)
        entry[name] = digest
    return entry


def _hydrate(tmp_path: Path, fake_s3, cfg: CloudConfig, entry: dict[str, str], stems_dir: Path):
    return hydrate_one(
        SID,
        data_dir=tmp_path / "data",
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={SID: entry},
        stems_dir=stems_dir,
    )


def _assert_reencoded(outcome, stems_dir: Path) -> None:
    assert outcome.status == "hydrated", outcome
    assert outcome.reason is not None and outcome.reason.startswith(HYDRATE_REENCODED), outcome.reason
    bundle = load_stem_bundle(SID, stems_dir=stems_dir)
    assert bundle.media_type == "audio/mpeg"
    assert sorted(p.name for p in stems_dir.iterdir()) == [SID], "a work or verify dir was left behind"
    names = sorted(p.name for p in (stems_dir / SID).iterdir())
    assert names == sorted(["manifest.json", *(f"{p}.mp3" for p in PARTS)]), names
    raw = json.loads((stems_dir / SID / "manifest.json").read_text())
    assert raw["codec"] == "mp3"
    assert raw["size_policy"]["mp3_settings"] == {p: f"lame_{k}kbps_cbr" for p, k in AT_RULE_192.items()}
    assert raw["recompressed_from"]["media_type"] == "audio/flac"
    for part in PARTS:
        frames = sf.read(str(stems_dir / SID / f"{part}.mp3"))[0].shape[0]
        assert FRAMES <= frames <= FRAMES + 2 * 1152, (part, frames)


def test_a_flac_stem_set_of_an_mp3_source_is_reencoded_on_arrival(tmp_path: Path, fake_s3, cfg: CloudConfig) -> None:
    """[if] a FLAC set of an MP3 source hydrates as FLAC [then] broken, [else stop]."""
    stems_dir = tmp_path / "stems"
    entry = _seed(fake_s3, cfg, _flac_set(tmp_path / "scratch", _source(tmp_path, "a")))

    _assert_reencoded(_hydrate(tmp_path, fake_s3, cfg, entry, stems_dir), stems_dir)


def test_a_flac_stem_set_is_refused_by_name_when_this_install_cannot_write_mp3(
    tmp_path: Path, fake_s3, cfg: CloudConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a no-encoder hydrate publishes FLAC or leaves a directory [then] broken, [else stop]."""
    stems_dir = tmp_path / "stems"
    entry = _seed(fake_s3, cfg, _flac_set(tmp_path / "scratch", _source(tmp_path, "a")))
    real = sf.available_formats
    monkeypatch.setattr(sf, "available_formats", lambda: {k: v for k, v in real().items() if k != "MP3"})

    outcome = _hydrate(tmp_path, fake_s3, cfg, entry, stems_dir)

    assert outcome.status == "error", outcome
    assert outcome.code == STEM_HYDRATE_POLICY_REFUSED
    assert outcome.reason is not None and outcome.reason.startswith(HYDRATE_REFUSED_NO_MP3_ENCODER), outcome.reason
    assert not stems_dir.exists() or list(stems_dir.iterdir()) == []
    with pytest.raises(StemBundleNotFoundError):
        load_stem_bundle(SID, stems_dir=stems_dir)

    # Control: with the encoder back the same fixture re-encodes, so the refusal came from the encoder.
    monkeypatch.undo()
    _assert_reencoded(_hydrate(tmp_path, fake_s3, cfg, entry, stems_dir), stems_dir)


def test_a_compliant_mp3_stem_set_hydrates_unchanged(tmp_path: Path, fake_s3, cfg: CloudConfig) -> None:
    """[if] a compliant MP3 set's bytes change on hydrate [then] broken, [else stop]."""
    stems_dir = tmp_path / "stems"
    files = _mp3_set(tmp_path / "scratch", _source(tmp_path, "a"))
    entry = _seed(fake_s3, cfg, files)

    outcome = _hydrate(tmp_path, fake_s3, cfg, entry, stems_dir)

    assert outcome.status == "hydrated", outcome
    assert outcome.reason is None
    assert {p.name: p.read_bytes() for p in (stems_dir / SID).iterdir()} == files


def test_a_flac_stem_set_whose_source_is_not_here_hydrates_unchanged(
    tmp_path: Path, fake_s3, cfg: CloudConfig
) -> None:
    """[if] a set the rule cannot judge (no source on disk) is refused or rewritten [then] broken, [else stop]."""
    stems_dir = tmp_path / "stems"
    files = _flac_set(tmp_path / "scratch", tmp_path / "absent.mp3")
    entry = _seed(fake_s3, cfg, files)

    outcome = _hydrate(tmp_path, fake_s3, cfg, entry, stems_dir)

    assert outcome.status == "hydrated", outcome
    assert outcome.reason is None
    assert load_stem_bundle(SID, stems_dir=stems_dir).media_type == "audio/flac"
    assert {p.name: p.read_bytes() for p in (stems_dir / SID).iterdir()} == files
