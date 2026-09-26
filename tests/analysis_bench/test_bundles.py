"""Fixture-bundle identity: what a consuming host is allowed to believe.

A bundle exists so that nucbox, agentbox and this Mac can each say "I scored
the same fixtures" and have that be checkable rather than asserted. Every test
here is about a way that claim can be false: a file that changed under the
manifest, a file that was added, a file that went missing, and two bundles that
share a version name while holding different fixtures.

The controls matter as much as the happy path. `test_sealed_bundle_verifies`
would pass against a `verify_bundle` that returned unconditionally, so each
refusal test below mutates exactly one thing and requires the specific refusal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.analysis_bench import bundles, stores


def _stage(root: Path, *, payload: bytes = b"fixture-audio-0") -> Path:
    """A staged bundle directory: payload files only, no metadata yet."""
    staged = root / "staged"
    (staged / "wav").mkdir(parents=True)
    (staged / "wav" / "track-a.wav").write_bytes(payload)
    (staged / "wav" / "track-b.wav").write_bytes(b"fixture-audio-1")
    (staged / "truth.json").write_text(json.dumps({"beats": {"track-a": [], "track-b": []}}))
    return staged


def _sealed(root: Path, **kwargs) -> Path:
    staged = _stage(root, **kwargs)
    bundles.seal_bundle(staged, lane="beatgrid", version="v1", manifest_extra={"fixture_count": 2})
    return staged


def test_sealed_bundle_verifies(tmp_path: Path) -> None:
    bundle = _sealed(tmp_path)
    report = bundles.verify_bundle(bundle)
    assert report["lane"] == "beatgrid"
    assert report["version"] == "v1"
    assert report["bundle_id"] == (bundle / bundles.ID_NAME).read_text().strip()


# REQ: NATIVE-11
def test_changed_payload_is_refused(tmp_path: Path) -> None:
    """The 28-percent-zero-filled-copy failure: same name, same size, other bytes."""
    bundle = _sealed(tmp_path)
    target = bundle / "wav" / "track-a.wav"
    target.write_bytes(b"\x00" * len(target.read_bytes()))
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.verify_bundle(bundle)
    assert "wav/track-a.wav" in str(excinfo.value)


def test_missing_payload_is_refused(tmp_path: Path) -> None:
    bundle = _sealed(tmp_path)
    (bundle / "wav" / "track-b.wav").unlink()
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.verify_bundle(bundle)
    assert "wav/track-b.wav" in str(excinfo.value)


def test_extra_payload_is_refused(tmp_path: Path) -> None:
    """A bundle carrying a file nobody checksummed is not this bundle."""
    bundle = _sealed(tmp_path)
    (bundle / "wav" / "track-c.wav").write_bytes(b"uninvited")
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.verify_bundle(bundle)
    assert "wav/track-c.wav" in str(excinfo.value)


def test_edited_manifest_is_refused(tmp_path: Path) -> None:
    """SHA256SUMS covers the manifest, so retitling a bundle cannot go unnoticed."""
    bundle = _sealed(tmp_path)
    manifest = json.loads((bundle / bundles.MANIFEST_NAME).read_text())
    manifest["version"] = "v2"
    (bundle / bundles.MANIFEST_NAME).write_text(json.dumps(manifest))
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.verify_bundle(bundle)
    assert bundles.MANIFEST_NAME in str(excinfo.value)


# REQ: NATIVE-11
def test_bundle_id_separates_two_bundles_sharing_a_version(tmp_path: Path) -> None:
    """The waveform lane's P1: `v1` alone cannot tell two fixture sets apart."""
    first = _sealed(tmp_path / "one")
    second = _sealed(tmp_path / "two", payload=b"fixture-audio-X")
    id_one = bundles.verify_bundle(first)["bundle_id"]
    id_two = bundles.verify_bundle(second)["bundle_id"]
    assert id_one != id_two
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.verify_bundle(second, expect_bundle_id=id_one)
    assert id_one in str(excinfo.value)


def test_identical_fixtures_reproduce_one_bundle_id(tmp_path: Path) -> None:
    """The other direction: the id is a function of content, not of build time."""
    first = _sealed(tmp_path / "one")
    second = _sealed(tmp_path / "two")
    assert bundles.verify_bundle(first)["bundle_id"] == bundles.verify_bundle(second)["bundle_id"]


def test_push_then_pull_round_trips_through_a_directory_store(tmp_path: Path) -> None:
    bundle = _sealed(tmp_path)
    store = stores.resolve_store(str(tmp_path / "store"))
    stores.push_bundle(bundle, store, lane="beatgrid", version="v1")
    dest = tmp_path / "pulled"
    pulled = stores.pull_bundle(store, lane="beatgrid", version="v1", dest=dest)
    assert (pulled / "wav" / "track-a.wav").read_bytes() == b"fixture-audio-0"
    assert bundles.verify_bundle(pulled)["bundle_id"] == bundles.verify_bundle(bundle)["bundle_id"]


# REQ: NATIVE-11
def test_pull_refuses_and_removes_a_corrupted_download(tmp_path: Path) -> None:
    """A store whose bytes rotted must not leave a scorable directory behind."""
    bundle = _sealed(tmp_path)
    store_root = tmp_path / "store"
    store = stores.resolve_store(str(store_root))
    stores.push_bundle(bundle, store, lane="beatgrid", version="v1")
    remote = store_root / "bench" / "beatgrid" / "v1" / "wav" / "track-a.wav"
    remote.write_bytes(b"\x00" * len(remote.read_bytes()))
    dest = tmp_path / "pulled"
    with pytest.raises(bundles.BundleError):
        stores.pull_bundle(store, lane="beatgrid", version="v1", dest=dest)
    assert not dest.exists()


REPO = Path(__file__).resolve().parents[2]


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    """The production CLI in a subprocess, under an environment we control outright.

    The two cases below are ABOUT the environment: what the harness does when a
    credential or a store URL is absent. Deleting the variable in-process with
    `monkeypatch` would have been testing the fake (Codex P1 BLOCKING,
    PR #1582), so the child gets a built environment instead -- PATH and HOME
    only, no `CF_R2_*`, no `MDT_BENCH_STORE`, whatever this host happens to
    export.
    """
    env = {name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ}
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run(
        [sys.executable, "-m", "apps.analysis_bench", *args],
        capture_output=True, text=True, env=env, cwd=REPO, check=False,
    )


def test_r2_store_names_the_missing_credential(tmp_path: Path) -> None:
    """No creds is a named refusal, never a silent fall back to somewhere else."""
    done = _cli("fixtures", "pull", "--lane", "beatgrid", "--version", "v1",
                "--store", "s3://af-main-r2/bench", "--dest", str(tmp_path / "pulled"))
    assert done.returncode == 2, done.stdout + done.stderr
    assert "CF_R2_ACCESS_KEY_ID" in done.stdout + done.stderr
    assert not (tmp_path / "pulled").exists()


def test_no_store_configured_is_a_named_refusal(tmp_path: Path) -> None:
    done = _cli("fixtures", "pull", "--lane", "beatgrid", "--version", "v1",
                "--dest", str(tmp_path / "pulled"))
    assert done.returncode == 2, done.stdout + done.stderr
    assert stores.STORE_ENV in done.stdout + done.stderr


def test_a_staged_manifest_description_survives_sealing(tmp_path: Path) -> None:
    """The builder's own description of the fixtures must not be overwritten."""
    staged = _stage(tmp_path)
    (staged / bundles.MANIFEST_NAME).write_text(
        json.dumps({"source": "rekordbox library, 45s excerpts", "window": "first drop"})
    )
    bundles.seal_bundle(staged, lane="beatgrid", version="v1", manifest_extra={"fixture_count": 2})
    manifest = bundles.verify_bundle(staged)
    assert manifest["source"] == "rekordbox library, 45s excerpts"
    assert manifest["window"] == "first drop"


def test_scoring_semantics_change_the_bundle_id(tmp_path: Path) -> None:
    """Identical audio scored under a different policy is a different question."""
    first = _stage(tmp_path / "one")
    second = _stage(tmp_path / "two")
    bundles.seal_bundle(
        first, lane="beatgrid", version="v1", manifest_extra={"window": "first drop"}
    )
    bundles.seal_bundle(
        second, lane="beatgrid", version="v1", manifest_extra={"window": "track start"}
    )
    assert bundles.verify_bundle(first)["bundle_id"] != bundles.verify_bundle(second)["bundle_id"]


def test_replacing_a_bundle_leaves_no_orphan_from_the_previous_push(tmp_path: Path) -> None:
    """A shrinking fixture set must not leave a file a later pull would resurrect."""
    store = stores.resolve_store(str(tmp_path / "store"))
    stores.push_bundle(_sealed(tmp_path / "big"), store, lane="beatgrid", version="v1")
    small = tmp_path / "small"
    (small / "wav").mkdir(parents=True)
    (small / "wav" / "track-a.wav").write_bytes(b"fixture-audio-0")
    (small / "truth.json").write_text(json.dumps({"beats": {"track-a": []}}))
    bundles.seal_bundle(small, lane="beatgrid", version="v1", manifest_extra={"fixture_count": 1})
    stores.push_bundle(small, store, lane="beatgrid", version="v1")
    pulled = stores.pull_bundle(store, lane="beatgrid", version="v1", dest=tmp_path / "pulled")
    assert not (pulled / "wav" / "track-b.wav").exists()


def test_a_rebuild_at_a_different_time_keeps_its_bundle_id(tmp_path: Path) -> None:
    """Provenance describes the build, not the question, so it is not identity."""
    first = _stage(tmp_path / "one")
    second = _stage(tmp_path / "two")
    bundles.seal_bundle(first, lane="beatgrid", version="v1", manifest_extra={
        "window": "first drop", "built_at": "2026-09-08T00:00:00Z", "built_from": "/mac/pack.py"})
    bundles.seal_bundle(second, lane="beatgrid", version="v1", manifest_extra={
        "window": "first drop", "built_at": "2026-09-09T13:00:00Z", "built_from": "/nuc/pack.py"})
    one, two = bundles.verify_bundle(first), bundles.verify_bundle(second)
    assert one["bundle_id"] == two["bundle_id"]
    assert one["built_at"] != two["built_at"], "provenance must survive in the manifest"


@pytest.mark.parametrize(
    "suffix", ["../escaped.wav", "wav/../../escaped.wav", "/etc/passwd", ""]
)
def test_a_store_key_that_climbs_out_of_the_bundle_is_refused(tmp_path: Path, suffix: str) -> None:
    """A store we hold credentials for is still not allowed to pick the path."""
    dest = tmp_path / "pulled"
    dest.mkdir()
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.safe_target(dest, suffix)
    assert repr(suffix) in str(excinfo.value)
    assert not (tmp_path / "escaped.wav").exists()


def test_a_store_key_inside_the_bundle_resolves(tmp_path: Path) -> None:
    """The control: the refusal above would pass against a function that always raised."""
    dest = tmp_path / "pulled"
    dest.mkdir()
    assert bundles.safe_target(dest, "wav/track-a.wav") == dest / "wav" / "track-a.wav"


def test_a_manifest_that_points_outside_the_bundle_is_refused(tmp_path: Path) -> None:
    """An escaping reference means the bundle_id does not cover what is read."""
    staged = _stage(tmp_path)
    (staged / bundles.MANIFEST_NAME).write_text(json.dumps({"reads": {"truth": "../truth.json"}}))
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.seal_bundle(staged, lane="beatgrid", version="v1")
    assert "../truth.json" in str(excinfo.value)
    assert not (staged / bundles.ID_NAME).exists()


def test_a_manifest_referencing_an_unchecksummed_file_is_refused(tmp_path: Path) -> None:
    """Inside the bundle but unhashed is no better: SHA256SUMS does not cover it."""
    staged = _stage(tmp_path)
    (staged / bundles.MANIFEST_NAME).write_text(
        json.dumps({"fixtures": [{"stable_id": "a", "wav": "wav/not-staged.wav"}]})
    )
    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.seal_bundle(staged, lane="beatgrid", version="v1")
    assert "not-staged.wav" in str(excinfo.value)
    assert "checksummed" in str(excinfo.value)


def test_a_manifest_referencing_a_checksummed_file_seals(tmp_path: Path) -> None:
    """The control: the two refusals above would pass against a blanket rejection."""
    staged = _stage(tmp_path)
    (staged / bundles.MANIFEST_NAME).write_text(
        json.dumps({"reads": {"truth": "truth.json"},
                    "fixtures": [{"stable_id": "a", "wav": "wav/track-a.wav"}]})
    )
    bundles.seal_bundle(staged, lane="beatgrid", version="v1")
    assert bundles.verify_bundle(staged)["reads"] == {"truth": "truth.json"}


def test_replacing_a_directory_store_bundle_leaves_no_staging_litter(tmp_path: Path) -> None:
    """The swap is copy-then-rename, so nothing half-copied may survive it."""
    store_root = tmp_path / "store"
    store = stores.resolve_store(str(store_root))
    stores.push_bundle(_sealed(tmp_path / "one"), store, lane="beatgrid", version="v1")
    stores.push_bundle(_sealed(tmp_path / "two"), store, lane="beatgrid", version="v1")
    live = store_root / "bench" / "beatgrid"
    assert [p.name for p in sorted(live.iterdir())] == ["v1"]
    bundles.verify_bundle(live / "v1")


def test_the_reads_map_the_real_packer_writes_seals(tmp_path: Path) -> None:
    """`checksums` is metadata and `scorer` is repo code; only `truth` is payload."""
    staged = _stage(tmp_path)
    (staged / bundles.MANIFEST_NAME).write_text(json.dumps({"reads": {
        "truth": "truth.json",
        "checksums": "SHA256SUMS",
        "scorer": "apps/analysis_bench/scorers/beatgrid.py (version stamped in every artifact)",
    }}))
    bundles.seal_bundle(staged, lane="beatgrid", version="v1")
    assert bundles.verify_bundle(staged)["reads"]["checksums"] == "SHA256SUMS"


def test_a_swapped_payload_and_checksums_cannot_keep_the_bundle_id(tmp_path: Path) -> None:
    """Replacing the audio AND its checksum line must not still satisfy the pin."""
    staged = _sealed(tmp_path)
    pinned = bundles.verify_bundle(staged)["bundle_id"]

    swapped = staged / "wav" / "track-a.wav"
    swapped.write_bytes(b"different-audio-entirely")
    sums = staged / bundles.SUMS_NAME
    rewritten = [
        f"{bundles.sha256_file(swapped)}  wav/track-a.wav"
        if line.endswith("wav/track-a.wav") else line
        for line in sums.read_text().splitlines()
    ]
    sums.write_text("\n".join(rewritten) + "\n")

    with pytest.raises(bundles.BundleError) as excinfo:
        bundles.verify_bundle(staged, expect_bundle_id=pinned)
    assert "disagrees with SHA256SUMS" in str(excinfo.value)
    assert "wav/track-a.wav" in str(excinfo.value)


def test_a_rollback_that_cannot_complete_keeps_the_retired_bundle(tmp_path: Path) -> None:
    """Deleting the backup while leaving the error path would lose the last good copy."""
    retired = tmp_path / ".retired-abc"
    retired.mkdir()
    (retired / bundles.ID_NAME).write_text("the-last-valid-bundle\n")
    unreachable = tmp_path / "no-such-parent" / "v1"
    assert stores.DirStore._restore(retired, unreachable) is False
    assert (retired / bundles.ID_NAME).read_text().strip() == "the-last-valid-bundle"


def test_a_rollback_that_completes_reports_the_backup_expendable(tmp_path: Path) -> None:
    """The control: a restore that worked must not leave the caller holding litter."""
    retired = tmp_path / ".retired-abc"
    retired.mkdir()
    (retired / bundles.ID_NAME).write_text("the-last-valid-bundle\n")
    target = tmp_path / "v1"
    assert stores.DirStore._restore(retired, target) is True
    assert (target / bundles.ID_NAME).read_text().strip() == "the-last-valid-bundle"
    assert not retired.exists()
