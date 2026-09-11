"""Bundle identity: pin what a ``waveform/v1`` bundle actually measures.

Codex P1 BLOCKING (PR #1536, thread on ``scripts/build_waveform_bundle.py:274``):
``--sample-size``, ``--seed``, or a changed library let the builder overwrite
``data/bench/waveform/v1`` with a DIFFERENT truth set while
``score.verify_bundle()`` accepted any self-consistent bundle merely named
``waveform/v1``, so two rounds could claim "the same fixture" while scoring
different tracks. The fix is an identity derived from the truth itself (seed,
sample size, sorted stable ids, per-track truth payload sha256), pinned by the
scorer (``tests/analysis_waveform/test_scorer_gates.py``) and guarded here by
the builder's overwrite refusal.

A second Codex P1 BLOCKING on the same PR (thread on
``scripts/build_waveform_bundle.py:105``): the identity above did not include
the track's own AUDIO payload, so a repaired, relinked or re-encoded track -
same ``stable_id``, same rekordbox truth tag, different bytes underneath -
left ``bundle_id`` unchanged. ``payload_sha256`` (the audio content hash, tags
stripped for mp3 via ``apps.shared.hashing.sha256_audio_payload``) closes that
gap; it is folded into ``compute_bundle_id`` the same way ``truth_sha256`` is.

Both functions under test are pure with respect to the rekordbox library:
``compute_bundle_id`` takes already-hashed truth rows, and
``_refuse_stale_overwrite`` reads a real (if synthetic) ``manifest.json`` from
a real tmp directory. Neither mocks or fakes production code; they exercise
the same functions ``build()`` calls, just without needing a live library on
this host (AGENTS.md: no mocks, but a missing dependency must fail explicitly
rather than being manufactured - ``_mapped_tracks()`` already does that and is
untouched by this fix).

  - [if] the same seed and size draw the same truth set [then] the id matches order, [else stop].
  - [if] the seed, size, a truth hash, or payload hash differs [then] the id differs, [else stop].
  - [if] an existing directory's id differs from the one about to write [then] refuse, [else stop].
  - [if] a rebuild (same id) hits an existing directory [then] it is allowed, [else stop].
  - [if] several tracks share one directory [then] that is the recorded audio_root, [else stop].
  - [if] one track is sampled [then] audio_root is its parent dir, not its own path, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import build_waveform_bundle as bwb

pytestmark = pytest.mark.requirement("NATIVE-06")


def _tracks(
    ids_and_hashes: dict[str, str], *, payload_hashes: dict[str, str] | None = None
) -> list[dict[str, str]]:
    payload_hashes = payload_hashes or {}
    return [
        {
            "stable_id": sid,
            "truth_sha256": digest,
            "payload_sha256": payload_hashes.get(sid, f"sha256:payload-{sid}"),
        }
        for sid, digest in ids_and_hashes.items()
    ]


# ----- compute_bundle_id ---------------------------------------------------


def test_same_seed_and_size_reproduce_the_same_id_regardless_of_track_order() -> None:
    tracks = _tracks({"a": "sha256:aaa", "b": "sha256:bbb"})
    first = bwb.compute_bundle_id(seed=20260908, sample_size=50, tracks=tracks)
    second = bwb.compute_bundle_id(seed=20260908, sample_size=50, tracks=list(reversed(tracks)))
    assert first == second
    assert first.startswith("sha256:")


def test_a_different_seed_changes_the_id() -> None:
    tracks = _tracks({"a": "sha256:aaa"})
    baseline = bwb.compute_bundle_id(seed=20260908, sample_size=50, tracks=tracks)
    changed = bwb.compute_bundle_id(seed=1, sample_size=50, tracks=tracks)
    assert baseline != changed


def test_a_different_sample_size_changes_the_id() -> None:
    tracks = _tracks({"a": "sha256:aaa"})
    baseline = bwb.compute_bundle_id(seed=20260908, sample_size=50, tracks=tracks)
    changed = bwb.compute_bundle_id(seed=20260908, sample_size=10, tracks=tracks)
    assert baseline != changed


def test_a_different_truth_hash_changes_the_id() -> None:
    """The case the P1 named directly: same seed and size, a different draw
    (or a library that changed underneath an unchanged seed/size pair)."""
    baseline = bwb.compute_bundle_id(
        seed=20260908, sample_size=1, tracks=_tracks({"a": "sha256:aaa"})
    )
    changed = bwb.compute_bundle_id(
        seed=20260908, sample_size=1, tracks=_tracks({"a": "sha256:different"})
    )
    assert baseline != changed


def test_a_different_stable_id_set_changes_the_id() -> None:
    baseline = bwb.compute_bundle_id(
        seed=20260908, sample_size=1, tracks=_tracks({"a": "sha256:aaa"})
    )
    changed = bwb.compute_bundle_id(
        seed=20260908, sample_size=1, tracks=_tracks({"z": "sha256:aaa"})
    )
    assert baseline != changed


def test_a_different_audio_payload_hash_changes_the_id() -> None:
    """The second Codex P1: a repaired, relinked or re-encoded track keeps its
    ``stable_id`` and its rekordbox truth tag, so only the audio payload hash
    can tell the two builds apart."""
    baseline = bwb.compute_bundle_id(
        seed=20260908,
        sample_size=1,
        tracks=_tracks({"a": "sha256:aaa"}, payload_hashes={"a": "sha256:payload-1"}),
    )
    changed = bwb.compute_bundle_id(
        seed=20260908,
        sample_size=1,
        tracks=_tracks({"a": "sha256:aaa"}, payload_hashes={"a": "sha256:payload-2"}),
    )
    assert baseline != changed


def test_an_identical_rebuild_including_payload_hash_reproduces_the_id() -> None:
    """The companion positive case: nothing - not even the payload hash -
    differs, so the id must reproduce exactly (guards against the payload
    hash being folded in a way that is nondeterministic or order-sensitive)."""
    tracks = _tracks(
        {"a": "sha256:aaa", "b": "sha256:bbb"},
        payload_hashes={"a": "sha256:payload-a", "b": "sha256:payload-b"},
    )
    first = bwb.compute_bundle_id(seed=20260908, sample_size=50, tracks=tracks)
    second = bwb.compute_bundle_id(seed=20260908, sample_size=50, tracks=list(reversed(tracks)))
    assert first == second


# ----- _refuse_stale_overwrite ----------------------------------------------


def _write_manifest(out: Path, *, bundle: str, bundle_id: str | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"bundle": bundle}
    if bundle_id is not None:
        payload["bundle_id"] = bundle_id
    (out / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def test_no_existing_manifest_is_not_an_overwrite(tmp_path: Path) -> None:
    bwb._refuse_stale_overwrite(tmp_path / "v1", bundle_name="waveform/v1", bundle_id="sha256:x")


def test_an_identical_rebuild_is_allowed(tmp_path: Path) -> None:
    out = tmp_path / "v1"
    _write_manifest(out, bundle="waveform/v1", bundle_id="sha256:x")
    bwb._refuse_stale_overwrite(out, bundle_name="waveform/v1", bundle_id="sha256:x")


def test_a_different_truth_set_under_the_same_directory_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "v1"
    _write_manifest(out, bundle="waveform/v1", bundle_id="sha256:x")
    with pytest.raises(SystemExit, match="--version"):
        bwb._refuse_stale_overwrite(out, bundle_name="waveform/v1", bundle_id="sha256:y")


def test_a_pre_identity_manifest_is_also_refused(tmp_path: Path) -> None:
    """A manifest written before this fix has no ``bundle_id`` at all; treat
    that the same as a mismatch rather than assuming it must be safe to replace."""
    out = tmp_path / "v1"
    _write_manifest(out, bundle="waveform/v1", bundle_id=None)
    with pytest.raises(SystemExit, match="--version"):
        bwb._refuse_stale_overwrite(out, bundle_name="waveform/v1", bundle_id="sha256:y")


def test_the_default_bundle_dir_still_names_v1() -> None:
    """The overwrite refusal exists so v1 is never silently replaced; the
    default directory it guards must actually be v1."""
    assert bwb.bundle_dir(Path("/tmp/data")).name == bwb.BUNDLE_VERSION == "v1"


def test_a_new_version_writes_a_different_directory() -> None:
    assert bwb.bundle_dir(Path("/tmp/data"), version="v2").name == "v2"
    assert bwb.bundle_dir(Path("/tmp/data"), version="v2") != bwb.bundle_dir(Path("/tmp/data"))


# ----- _common_audio_root ---------------------------------------------------


def test_several_tracks_under_one_directory_share_that_root() -> None:
    root = bwb._common_audio_root(
        ["/library/DJ Music/a.mp3", "/library/DJ Music/sub/b.mp3", "/library/DJ Music/c.mp3"]
    )
    assert root == Path("/library/DJ Music")


def test_a_single_sampled_track_roots_at_its_own_parent_not_its_own_path() -> None:
    """``os.path.commonpath`` of one full path returns that path itself, which
    would turn the one track's relative audio_path into an empty string;
    ``_common_audio_root`` must use the parent instead."""
    root = bwb._common_audio_root(["/library/DJ Music/only.mp3"])
    assert root == Path("/library/DJ Music")
