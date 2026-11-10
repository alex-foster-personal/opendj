"""reencode_staged_bundle on a private staging root (R2 hydrate, #9665, STEM-8231).

[if] an ELIGIBLE FLAC bundle of an MP3 source is re-encoded [then] it is MP3 in place and the root holds only stable_id, [else stop].
[if] the candidate is not ELIGIBLE [then] ValueError and nothing is written, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.stems import recompress as rc
from apps.stems.artifacts import load_stem_bundle
from tests.stems.recompress_support import _flac_bundle, _snapshot, _source

pytestmark = [pytest.mark.requirement("STEM-8231"), pytest.mark.requires_audio_stack]


def test_reencode_staged_bundle_rewrites_a_private_copy_and_leaves_no_work_dir(tmp_path: Path) -> None:
    """[if] ELIGIBLE FLAC is re-encoded [then] MP3 in place and no work dir left, [else stop]."""
    root = tmp_path / "verify"
    root.mkdir()
    source = _source(tmp_path, "a")
    _flac_bundle_in(root, "mp3src", source)
    candidate = rc.classify(root, "mp3src", str(source))
    assert candidate.verdict == rc.ELIGIBLE

    outcome = rc.reencode_staged_bundle(candidate)

    assert sorted(p.name for p in root.iterdir()) == ["mp3src"]
    assert outcome.media_type_before == "audio/flac"
    assert outcome.after_bytes < outcome.before_bytes
    assert load_stem_bundle("mp3src", roots=[root]).media_type == "audio/mpeg"


def test_reencode_staged_bundle_refuses_a_non_eligible_candidate(tmp_path: Path) -> None:
    """[if] a non-ELIGIBLE candidate is re-encoded [then] ValueError and root unchanged, [else stop]."""
    root = tmp_path / "verify"
    root.mkdir()
    source = _source(tmp_path, "b", kbps=None)
    bundle = _flac_bundle_in(root, "wavsrc", source)
    candidate = rc.classify(root, "wavsrc", str(source))
    assert candidate.verdict == rc.SKIP_LOSSLESS_SOURCE
    before = _snapshot(bundle)

    with pytest.raises(ValueError, match="not eligible"):
        rc.reencode_staged_bundle(candidate)

    assert _snapshot(bundle) == before
    assert sorted(p.name for p in root.iterdir()) == ["wavsrc"]


def _flac_bundle_in(root: Path, stable_id: str, source: Path) -> Path:
    """Build the support FLAC bundle, then move it into a plain staging root."""
    data = root.parent / f"data-{stable_id}"
    (data / "state" / "stems").mkdir(parents=True)
    built = _flac_bundle(data, stable_id, source)
    return built.rename(root / stable_id)
