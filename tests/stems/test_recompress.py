"""apps.stems.recompress: lossless bundles of lossy sources become MP3 (STEM-58).

[if] a FLAC bundle's source is MP3 [then] a live pass rewrites it as a v3 MP3 bundle the real
  reader loads, every part at or under the source, decoded frames matching, [else stop].
[if] the source is lossless, or the bundle is already MP3, or no source is on disk [then] it is
  left alone and counted, [else stop].
[if] a stems job is queued or running, or jobs.db is missing [then] the live pass refuses and
  changes nothing, [else stop].
[if] a bundle is swapped [then] the old directory is in the backup, byte for byte, [else stop].
[if] a bundle fails to re-encode [then] the stem root is unchanged, [else stop].
[if] a stems job appears after the pass started [then] the next swap and the rest of the pass
  stop with exit 3, and bundles already swapped stay swapped, [else stop].
[if] the work directory cannot be removed [then] the error names it and the failure that led
  there, [else stop].
[if] the data dir path holds a character a URI treats specially [then] the read-only database
  opens still work, [else stop].

[if] a bundle's manifest says FLAC was chosen (a FLAC tier's preset, or an explicit codec from a
  policy-aware writer) [then] it is not eligible and a live pass leaves it byte for byte; legacy
  bundles (no codec recorded, or the STEM-51 fallback) still are, [else stop].
[if] a stems job publishes a bundle while the pass encodes it [then] the swap refuses and the
  published bundle stays exactly as published, nothing in the backup, [else stop].
[if] any writer holds the bundle lock [then] a publish (Modal/relay route, local worker, the
  farm's --stems-dest local) and the re-compress snapshot and swap wait for it, and land once it is
  released, [else stop].
[if] the bundle was replaced between plan() and the snapshot by one that is no longer eligible
  [then] it is left untouched; an unchanged candidate is still converted, [else stop].
[if] state.db relinks the track between plan() and the snapshot (to a lossless file, or to any
  other source) [then] the bundle is left untouched; an unchanged library path is still converted,
  and from that path, [else stop].

Regression lines:
- if a job that went live mid-pass does not stop the next swap then broken
- if the re-check stops a pass with no live job then broken (the overshoot)
- if a leftover work directory is swallowed in silence then broken
- if a Windows path yields a URI SQLite rejects then broken

Real FLAC and MP3 bytes throughout (soundfile), real sqlite, no mocks.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

try:
    import numpy as np
    import soundfile as sf
except ImportError:
    # Never a module-level skip: collection must reach the requires_audio_stack
    # marker below, which tests/conftest.py turns into an UNAVAILABLE capability
    # report per test (Codex on #6821). Every use of np/sf is inside a test.
    np = sf = None  # type: ignore[assignment]

from apps.stems import recompress as rc
from apps.stems.artifacts import load_stem_bundle
from apps.stems.stem_encode import encode_mp3
from apps.cloud.stem_index import INDEX_CACHE_FILENAME, MANIFEST_FILENAME, entry_from_bundle_dir, load_cached_index, save_cached_index
from tests.stems.recompress_support import (
    FRAMES,
    PARTS,
    RATE,
    SECONDS,
    _data_dir,
    _flac_bundle,
    _fresh_parts,
    _noise,
    _publish_result,
    _snapshot,
    _source,
    _stale_flac_index_entry,
    _the_two_flac_publishers,
)

pytestmark = [pytest.mark.requirement("STEM-58"), pytest.mark.requires_audio_stack]


def _verdicts(data: Path) -> dict[str, str]:
    return {c.stable_id: c.verdict for c in rc.plan(data)}


def test_plan_picks_only_lossless_bundles_of_lossy_sources(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    _flac_bundle(data, "mp3src", _source(tmp_path, "a"))
    _flac_bundle(data, "wavsrc", _source(tmp_path, "b", kbps=None))
    gone = _source(tmp_path, "c")
    _flac_bundle(data, "nosrc", gone)
    gone.unlink()

    assert _verdicts(data) == {
        "mp3src": rc.ELIGIBLE,
        "wavsrc": rc.SKIP_LOSSLESS_SOURCE,
        "nosrc": rc.SKIP_NO_SOURCE,
    }


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "mp3src", _source(tmp_path, "a"))
    before = _snapshot(bundle)

    assert rc.main(["--data-dir", str(data)]) == 0

    assert _snapshot(bundle) == before
    assert not (data / "state" / "stems-recompress-backup").exists()


@pytest.mark.parametrize("limit", ["-1", "0"])
def test_a_negative_limit_is_refused_before_any_write(tmp_path: Path, limit: str) -> None:
    """[if] ``--live --limit`` is negative [then] the run stops at parsing with nothing written, [else] 0 is a valid count.

    ``eligible[:-1]`` is every bundle but the last, so ``--limit -1`` rewrote
    nearly the whole library (Sol on #6820). MUTATION TARGETS: ``type=int``
    again rewrites a bundle; refusing 0 too fails the control row.
    """
    data = _data_dir(tmp_path)
    bundles = [_flac_bundle(data, f"mp3src{i}", _source(tmp_path, f"s{i}")) for i in range(2)]
    before = [_snapshot(b) for b in bundles]
    backup = tmp_path / "backup"
    argv = ["--data-dir", str(data), "--live", "--backup-dir", str(backup), "--limit", limit]

    if limit == "-1":
        with pytest.raises(SystemExit) as exit_info:
            rc.main(argv)
        assert exit_info.value.code == 2
    else:
        assert rc.main(argv) == 0

    assert [_snapshot(b) for b in bundles] == before
    assert not backup.exists()


def test_live_pass_rewrites_as_mp3_under_the_source_and_keeps_a_backup(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    source = _source(tmp_path, "a")
    bundle = _flac_bundle(data, "mp3src", source)
    old = _snapshot(bundle)
    backup = tmp_path / "backup"

    assert rc.main(["--data-dir", str(data), "--live", "--backup-dir", str(backup)]) == 0

    loaded = load_stem_bundle("mp3src", roots=[data / "state" / "stems"])
    assert loaded.media_type == "audio/mpeg"
    assert (loaded.alignment.frame_count, loaded.alignment.channels) == (FRAMES, 2)
    for part in PARTS:
        path = bundle / f"{part}.mp3"
        assert path.stat().st_size <= source.stat().st_size, part
        assert FRAMES <= sf.info(str(path)).frames <= FRAMES + rc.MP3_MAX_EXTRA_FRAMES
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["schema_version"] == 3
    assert manifest["model"]["name"] == "hdemucs_mmi"
    assert manifest["recompressed_from"]["media_type"] == "audio/flac"
    assert manifest["size_policy"]["violations"] == []
    assert not list(bundle.glob("*.flac"))
    assert _snapshot(backup / "stems" / "mp3src") == old
    # A second pass finds nothing left to do: the new parts are at STEM-8231.
    assert _verdicts(data) == {"mp3src": rc.SKIP_AT_POLICY}


# ----- STEM-8231: shrink MP3 bundles made under the old per-part rule ----------
def _mp3_bundle(data: Path, stable_id: str, source: Path, kbps: dict[str, int], *, schema: int = 3) -> Path:
    """A schema v3 MP3 bundle with each part CBR at ``kbps[part]``; ``schema=1``
    writes the old shape with no ``audio`` block (length derived from a header)."""
    bundle = data / "state" / "stems" / stable_id
    bundle.mkdir()
    for i, part in enumerate(PARTS):
        encode_mp3(_noise(i), RATE, bundle / f"{part}.mp3", cbr_kbps=kbps[part])
    manifest = {
        "schema_version": 3,
        "stable_id": stable_id,
        "layout": "demucs4",
        "model": {"name": "hdemucs_mmi", "version": "4.0.1"},
        "source": {"path": str(source), "sha256": "0" * 64},
        "files": {part: f"{part}.mp3" for part in PARTS},
        "preset": {"tag": "hdemucs_mmi-ov0.25"},
        "audio": {"sample_rate": RATE, "frame_count": FRAMES, "channels": 2},
        "codec": "mp3",
    }
    if schema == 1:
        manifest = {k: manifest[k] for k in ("stable_id", "model", "source", "files", "layout")} | {"schema_version": 1}
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


OLD_RULE = {part: 320 for part in PARTS}  # STEM-57: each part at 320 CBR
AT_RULE_192 = {"vocals": 64, "other": 64, "drums": 96, "bass": 96}  # STEM-8231 for a 192 kbps source


@pytest.mark.requirement("STEM-8231")
def test_an_old_rule_mp3_bundle_is_eligible_and_a_compliant_one_is_not(tmp_path: Path) -> None:
    """[if] a 320-per-part MP3 bundle is skipped or a STEM-8231 one is re-encoded [then] broken, [else stop]."""
    data = _data_dir(tmp_path)
    _mp3_bundle(data, "oldrule", _source(tmp_path, "a"), OLD_RULE)
    _mp3_bundle(data, "atrule", _source(tmp_path, "b"), AT_RULE_192)

    assert _verdicts(data) == {"oldrule": rc.ELIGIBLE, "atrule": rc.SKIP_AT_POLICY}


@pytest.mark.requirement("STEM-8231")
@pytest.mark.parametrize("schema", [3, 1])
def test_live_pass_shrinks_an_old_rule_mp3_bundle_to_the_whole_set_rule(tmp_path: Path, schema: int) -> None:
    """[if] the shrink leaves a part over STEM-8231, under-floors bass/drums, or changes frames [then] broken, [else stop]."""
    data = _data_dir(tmp_path)
    source = _source(tmp_path, "a")  # 192 kbps
    bundle = _mp3_bundle(data, "oldrule", source, OLD_RULE, schema=schema)
    old = _snapshot(bundle)
    backup = tmp_path / "backup"

    assert rc.main(["--data-dir", str(data), "--live", "--backup-dir", str(backup)]) == 0

    loaded = load_stem_bundle("oldrule", roots=[data / "state" / "stems"])
    assert loaded.media_type == "audio/mpeg"
    assert loaded.alignment.frame_count == FRAMES
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["size_policy"]["mp3_settings"] == {p: f"lame_{k}kbps_cbr" for p, k in AT_RULE_192.items()}
    assert manifest["recompressed_from"]["media_type"] == "audio/mpeg"
    mpeg1_l3_kbps = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    for part in PARTS:
        data_bytes = (bundle / f"{part}.mp3").read_bytes()
        assert mpeg1_l3_kbps[data_bytes[2] >> 4] == AT_RULE_192[part], part
        decoded = len(sf.read(str(bundle / f"{part}.mp3"), dtype="float32")[0])
        assert FRAMES <= decoded <= FRAMES + rc.MP3_MAX_EXTRA_FRAMES, part
    assert _dir_bytes_of(bundle) < sum(len(b) for b in old.values()) / 2
    assert _snapshot(backup / "stems" / "oldrule") == old
    assert _verdicts(data) == {"oldrule": rc.SKIP_AT_POLICY}


def _dir_bytes_of(path: Path) -> int:
    return sum(f.stat().st_size for f in path.iterdir())


@pytest.mark.parametrize("status", ["queued", "running", "cancelling"])
def test_live_pass_refuses_while_a_stems_job_is_live(tmp_path: Path, status: str) -> None:
    data = _data_dir(tmp_path, jobs=[("stems.separate", status)])
    bundle = _flac_bundle(data, "mp3src", _source(tmp_path, "a"))
    before = _snapshot(bundle)

    assert rc.main(["--data-dir", str(data), "--live"]) == 3
    assert _snapshot(bundle) == before


def test_finished_and_other_jobs_do_not_block(tmp_path: Path) -> None:
    data = _data_dir(tmp_path, jobs=[("stems.separate", "succeeded"), ("lyrics.index", "running")])

    rc.assert_no_live_stems_job(data, missing_ok=False)


def test_missing_jobs_db_refuses_unless_told_it_never_existed(tmp_path: Path) -> None:
    data = _data_dir(tmp_path, jobs=None)
    bundle = _flac_bundle(data, "mp3src", _source(tmp_path, "a"))
    before = _snapshot(bundle)

    assert rc.main(["--data-dir", str(data), "--live"]) == 3
    assert _snapshot(bundle) == before
    assert rc.main(["--data-dir", str(data), "--live", "--no-jobs-db-ok"]) == 0
    assert load_stem_bundle("mp3src", roots=[data / "state" / "stems"]).media_type == "audio/mpeg"


def test_a_failed_bundle_leaves_the_stem_root_unchanged(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "mp3src", _source(tmp_path, "a"))
    # A part shorter than the manifest declares: decodes, but must not be swapped in.
    sf.write(bundle / "bass.flac", _noise(9)[: FRAMES // 2], RATE, format="FLAC", subtype="PCM_16")
    before = _snapshot(bundle)

    assert rc.main(["--data-dir", str(data), "--live"]) == 1

    assert _snapshot(bundle) == before
    assert [p.name for p in (data / "state" / "stems").iterdir()] == ["mp3src"]


def _state_db(data: Path, rows: list[tuple[str, str, str | None]]) -> None:
    db = sqlite3.connect(data / "state" / "state.db")
    db.execute("CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, file_path TEXT, deleted_at TEXT)")
    db.executemany("INSERT INTO tracks VALUES (?, ?, ?)", rows)
    db.commit()
    db.close()


def test_library_path_wins_but_a_soft_deleted_row_is_ignored(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    moved = _source(tmp_path, "moved")
    lossless = _source(tmp_path, "lossless", kbps=None)
    # Both manifests point at a file that is gone; the library knows where each track is now.
    for stable_id in ("live", "tombstoned"):
        gone = _source(tmp_path, f"gone-{stable_id}")
        _flac_bundle(data, stable_id, gone)
        gone.unlink()
    _state_db(
        data,
        [("live", str(moved), None), ("tombstoned", str(lossless), "2026-10-01T00:00:00Z")],
    )

    candidates = {c.stable_id: c for c in rc.plan(data)}

    assert candidates["live"].verdict == rc.ELIGIBLE
    assert candidates["live"].source_path == moved
    # A deleted library row is not the track's current file: fall back to the manifest.
    assert candidates["tombstoned"].verdict == rc.SKIP_NO_SOURCE


def _add_job(data: Path, kind: str, status: str) -> None:
    db = sqlite3.connect(data / "state" / "jobs.db")
    db.execute("INSERT INTO jobs VALUES (?, ?, ?)", (f"late-{kind}-{status}", kind, status))
    db.commit()
    db.close()


def _two_bundles(tmp_path: Path) -> tuple[Path, Path, Path]:
    data = _data_dir(tmp_path)
    source = _source(tmp_path, "a")
    return data, _flac_bundle(data, "a-first", source), _flac_bundle(data, "b-second", source)


def test_a_stems_job_appearing_mid_pass_stops_the_next_swap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a stems job is queued after the first bundle is swapped [then] the real pass stops before the second swap, [else stop].

    The real pass runs in a thread. The test holds the second bundle's lock
    (another stripe than the first's), so the pass swaps the first bundle and
    then waits at the second's snapshot; the test queues a real jobs row only
    once the first bundle is MP3 on disk, then releases the lock. No pass
    function is replaced (Codex P1 on #6820).

    MUTATION TARGET: drop the per-swap job re-check, and the second bundle is
    swapped while a stems job is queued.
    """
    import threading

    from apps.stems.bundle_lock import bundle_lock, bundle_lock_path

    data, first, second = _two_bundles(tmp_path)
    assert bundle_lock_path(data, first.name) != bundle_lock_path(data, second.name), "control: two stripes"
    second_before = _snapshot(second)
    report = tmp_path / "report.json"
    exit_codes: list[int] = []

    def run_pass() -> None:
        exit_codes.append(rc.main(["--data-dir", str(data), "--live", "--json", str(report)]))

    with bundle_lock(second):
        worker = threading.Thread(target=run_pass)
        worker.start()
        _wait_for(
            lambda: (first / "manifest.json").is_file() and (first / "vocals.mp3").is_file(),
            what="the first bundle swapped to MP3",
        )
        _add_job(data, "stems.separate", "queued")
    worker.join(timeout=120)
    assert not worker.is_alive(), "the pass finished"
    assert exit_codes == [3]

    roots = [data / "state" / "stems"]
    assert load_stem_bundle("a-first", roots=roots).media_type == "audio/mpeg"
    assert _snapshot(second) == second_before
    assert sorted(p.name for p in roots[0].iterdir()) == ["a-first", "b-second"], "no work directory left"
    assert not any(rc.work_parent(roots[0]).glob(".recompress-*")), "no work directory left beside the root"
    stderr = capsys.readouterr().err
    assert "stopped before swapping b-second (2 of 2)" in stderr
    assert "stems.separate queued" in stderr
    written = json.loads(report.read_text())
    assert [o["stable_id"] for o in written["outcomes"]] == ["a-first"]
    assert "b-second" in written["stopped"]


def test_with_no_job_appearing_every_bundle_is_swapped(tmp_path: Path) -> None:
    """The overshoot control: the per-swap re-check must not stop a quiet pass."""
    data, _first, _second = _two_bundles(tmp_path)
    # A finished stems job and a live job of another kind are not a reason to stop.
    _add_job(data, "stems.separate", "succeeded")
    _add_job(data, "lyrics.index", "running")
    report = tmp_path / "report.json"

    assert rc.main(["--data-dir", str(data), "--live", "--json", str(report)]) == 0

    assert _verdicts(data) == {"a-first": rc.SKIP_AT_POLICY, "b-second": rc.SKIP_AT_POLICY}
    assert json.loads(report.read_text())["stopped"] is None


def test_before_swap_runs_on_a_verified_bundle_and_a_raise_swaps_nothing(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "mp3src", _source(tmp_path, "a"))
    before = _snapshot(bundle)
    (candidate,) = rc.plan(data)
    seen: list[list[str]] = []

    def refuse() -> None:
        (work,) = rc.work_parent(data / "state" / "stems").glob(".recompress-mp3src-*")
        seen.append(sorted(p.name for p in (work / "mp3src").iterdir()))
        raise rc.StemsJobLiveError("1 stems job(s) live: stems.separate running (job-x)")

    with pytest.raises(rc.StemsJobLiveError, match=r"stems\.separate running"):
        rc.recompress_bundle(candidate, tmp_path / "backup", before_swap=refuse)

    # It ran once, with the whole new bundle already written and verified.
    assert seen == [sorted([*(f"{part}.mp3" for part in PARTS), "manifest.json"])]
    assert _snapshot(bundle) == before
    assert [p.name for p in (data / "state" / "stems").iterdir()] == ["mp3src"]
    assert not (tmp_path / "backup").exists()


def test_a_cache_scan_mid_pass_sees_only_the_real_bundle(tmp_path: Path) -> None:
    """[if] a cache-budget scan runs while a bundle is being re-compressed [then] it sees only the real bundle, [else stop].

    The snapshot and the new parts are staged beside the stem root, never
    inside it, so ``scan_bundles`` (what cache eviction and the upload sweep
    walk) never counts the workspace or queues it as a stable_id (Codex on
    PR #6821). The control proves the workspace really is on disk then.

    MUTATION TARGET: make the work directory under the stem root again and the
    scan names a second ``.recompress-*`` bundle.
    """
    from apps.cloud.stem_bundles import scan_bundles

    data = _data_dir(tmp_path)
    root = data / "state" / "stems"
    _flac_bundle(data, "mp3src", _source(tmp_path, "a"))
    (candidate,) = rc.plan(data)
    seen: dict[str, Any] = {}

    def scan_mid_pass() -> None:
        seen["scanned"] = [bundle.stable_id for bundle in scan_bundles(root)]
        seen["work"] = [p.name for p in rc.work_parent(root).glob(".recompress-mp3src-*/mp3src")]

    outcome = rc.recompress_bundle(candidate, tmp_path / "backup", before_swap=scan_mid_pass)

    assert seen["scanned"] == ["mp3src"], seen
    assert seen["work"] == ["mp3src"], "control: the staged bundle is on disk while the scan runs"
    assert outcome.stable_id == "mp3src" and (root / "mp3src" / "vocals.mp3").is_file(), "the swap landed"
    assert not list(rc.work_parent(root).iterdir()), "the work directory is removed after the swap"


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a directory the user cannot empty")
def test_a_work_directory_that_cannot_be_removed_is_reported(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "mp3src", _source(tmp_path, "a"))
    before = _snapshot(bundle)
    (candidate,) = rc.plan(data)
    locked: list[Path] = []

    def lock_the_new_bundle_then_fail() -> None:
        (work,) = rc.work_parent(data / "state" / "stems").glob(".recompress-mp3src-*")
        locked.append(work / "mp3src")
        locked[0].chmod(0o500)  # its files can no longer be unlinked, so rmtree really fails
        raise RuntimeError("the failure that led to cleanup")

    try:
        with pytest.raises(rc.WorkspaceCleanupError) as raised:
            rc.recompress_bundle(candidate, tmp_path / "backup", before_swap=lock_the_new_bundle_then_fail)
        message = str(raised.value)
        assert str(locked[0].parent) in message, message
        assert "the failure that led to cleanup" in message, message
        assert locked[0].is_dir(), "control: the work directory really is still on disk"
        assert _snapshot(bundle) == before
    finally:
        for path in locked:
            path.chmod(0o700)


def test_readonly_uri_for_a_windows_path_is_one_sqlite_accepts() -> None:
    uri = rc.readonly_sqlite_uri(PureWindowsPath(r"C:\Users\dev\Open DJ\state\jobs.db"))

    assert uri == "file:///C:/Users/dev/Open%20DJ/state/jobs.db?mode=ro"
    assert "\\" not in uri


def test_databases_open_under_a_data_dir_with_uri_special_characters(tmp_path: Path) -> None:
    """`#` and `?` end the path in a URI unless escaped; pasted after `file:` they
    made SQLite look for a different file."""
    odd = tmp_path / "library #1 (what?)"
    odd.mkdir()
    data = _data_dir(odd, jobs=[("stems.separate", "running")])
    moved = _source(tmp_path, "moved")
    gone = _source(tmp_path, "gone")
    _flac_bundle(data, "mp3src", gone)
    gone.unlink()
    _state_db(data, [("mp3src", str(moved), None)])

    # state.db is read: the library row is what makes this bundle eligible.
    assert {c.stable_id: (c.verdict, c.source_path) for c in rc.plan(data)} == {"mp3src": (rc.ELIGIBLE, moved)}
    # jobs.db is read: the running job is found, not reported as an unreadable database.
    with pytest.raises(rc.StemsJobLiveError, match=r"1 stems job\(s\) live: stems.separate running"):
        rc.assert_no_live_stems_job(data, missing_ok=False)


# ----- provenance: deliberate lossless bundles stay lossless (Codex P2 on #6820) -----


def _bundle_with(data: Path, stable_id: str, source: Path, manifest: dict[str, Any], *, ext: str = "flac") -> Path:
    """A real lossless bundle (FLAC or WAV parts) with the given manifest fields on top."""
    bundle = data / "state" / "stems" / stable_id
    bundle.mkdir()
    for i, part in enumerate(PARTS):
        if ext == "flac":
            sf.write(bundle / f"{part}.flac", _noise(i), RATE, format="FLAC", subtype="PCM_16")
        else:
            sf.write(bundle / f"{part}.wav", _noise(i), RATE, subtype="PCM_16")
    body = {
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {"path": str(source), "sha256": "0" * 64},
        "files": {part: f"{part}.{ext}" for part in PARTS},
        "audio": {"sample_rate": RATE, "frame_count": FRAMES, "channels": 2},
        **manifest,
    }
    if body["schema_version"] == 1:
        del body["audio"]  # schema v1 is WAV-only and carries no audio block
    (bundle / "manifest.json").write_text(json.dumps(body))
    return bundle


_V3 = {"schema_version": 3, "layout": "demucs4"}
_FLAC_POLICY: dict[str, Any] = {"source_bytes": 1, "source_kbps": 192.0, "mp3_settings": None, "part_bytes": {}, "violations": []}


def _provenance_library(tmp_path: Path) -> Path:
    data = _data_dir(tmp_path)
    source = _source(tmp_path, "a")
    # Legacy, eligible: farm schema v2 of an M-tier preset (no codec recorded).
    _flac_bundle(data, "legacy-farm-v2", source)
    # Legacy, eligible: the local worker's WAV bundle from before the codec policy.
    _bundle_with(data, "legacy-local-wav", source, {"schema_version": 1}, ext="wav")
    # Legacy, eligible: the STEM-51 fallback, which wanted MP3 and had no encoder.
    _bundle_with(
        data,
        "stem51-fallback",
        source,
        {
            **_V3,
            "codec": "flac",
            "size_policy": {**_FLAC_POLICY, "policy_codec": "mp3", "codec_fallback": "no mp3 encoder"},
        },
    )
    # Deliberate: tier L's preset (farm schema v2, before STEM-75 recorded a codec).
    _flac_bundle_l = _flac_bundle(data, "tier-l-v2", source)
    raw = json.loads((_flac_bundle_l / "manifest.json").read_text())
    raw["preset"] = {"tag": "htdemucs_ft-ov0.25"}
    (_flac_bundle_l / "manifest.json").write_text(json.dumps(raw))
    # Deliberate: a policy-aware writer recorded FLAC (tier L or --stem-codec flac, STEM-75).
    _bundle_with(
        data,
        "explicit-flac-v3",
        source,
        {**_V3, "codec": "flac", "size_policy": _FLAC_POLICY, "preset": {"tag": "hdemucs_mmi-ov0.25"}},
    )
    return data


def test_only_legacy_lossless_bundles_are_eligible(tmp_path: Path) -> None:
    data = _provenance_library(tmp_path)

    assert _verdicts(data) == {
        "legacy-farm-v2": rc.ELIGIBLE,
        "legacy-local-wav": rc.ELIGIBLE,
        "stem51-fallback": rc.ELIGIBLE,
        "tier-l-v2": rc.SKIP_DELIBERATE_LOSSLESS,
        "explicit-flac-v3": rc.SKIP_DELIBERATE_LOSSLESS,
    }


def test_a_live_pass_leaves_deliberate_flac_byte_for_byte(tmp_path: Path) -> None:
    data = _provenance_library(tmp_path)
    stems = data / "state" / "stems"
    kept = {sid: _snapshot(stems / sid) for sid in ("tier-l-v2", "explicit-flac-v3")}

    assert rc.main(["--data-dir", str(data), "--live"]) == 0

    for sid, before in kept.items():
        assert _snapshot(stems / sid) == before, sid
    # The overshoot control: every legacy bundle WAS migrated.
    for sid in ("legacy-farm-v2", "legacy-local-wav", "stem51-fallback"):
        assert load_stem_bundle(sid, roots=[stems]).media_type == "audio/mpeg", sid


# ----- the bundle lock: no writer clobbers another (Codex P1 on #6820) --------------

_PUBLISH_CHILD = r"""
import json, sys
from pathlib import Path
from apps.stems import bundle_publish

data_dir, sid, parts_dir, marker = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
result = {
    "stems": {p: (parts_dir / f"{p}.flac").read_bytes() for p in bundle_publish.STEM_PARTS},
    "stem_ext": "flac",
    "stem_codec": "flac",
    "size_policy": None,
    "source_sha256": "1" * 64,
    "audio": {"sample_rate": 44100, "frame_count": int(sys.argv[5]), "channels": 2},
}
marker.write_text("publishing")
bundle_publish.publish_stems_local(
    sid, Path("/music/a.mp3"), result, bundle_publish.PRESETS["hdemucs_mmi-ov0.25"], data_dir,
    requested_codec="flac",
)
"""


def _wait_for(predicate: Any, *, what: str, timeout_s: float = 60.0) -> None:
    import time

    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


def test_a_publish_in_another_process_waits_for_the_bundle_lock(tmp_path: Path) -> None:
    """The publisher a stems job runs holds the shared lock: while another
    holder has it, a real child process's publish does not touch the bundle."""
    import subprocess
    import sys
    import time

    from apps.stems.bundle_lock import bundle_lock

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "busy", _source(tmp_path, "a"))
    before = _snapshot(bundle)
    marker = tmp_path / "child-publishing"
    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(repo)}
    args = [sys.executable, "-c", _PUBLISH_CHILD, str(data), "busy", str(_fresh_parts(tmp_path, 7)), str(marker)]

    with bundle_lock(bundle):
        child = subprocess.Popen([*args, str(FRAMES)], cwd=repo, env=env)
        try:
            _wait_for(marker.exists, what="the child to reach its publish")
            time.sleep(1.0)  # a publish of four small parts takes milliseconds when unlocked
            assert child.poll() is None, "the child finished while the lock was held"
            assert _snapshot(bundle) == before, "the child wrote into a locked bundle"
        except BaseException:
            child.kill()
            raise
    assert child.wait(timeout=60) == 0
    assert _snapshot(bundle) != before, "control: once released, the publish lands"


def test_a_publish_stages_its_parts_before_it_takes_the_bundle_lock(tmp_path: Path) -> None:
    """[if] another writer holds the bundle lock [then] a publish writes its parts aside but swaps nothing in until release, [else stop].

    The lock is striped, so a publish writing four parts under it made every
    hydrate or publish of an id on the same stripe wait out the disk writes
    (Codex on #6821). Control: once released, the publish lands with the
    staged bytes.

    MUTATION TARGET: move ``write(staging)`` back under ``bundle_lock`` in
    ``publish_bundle`` (no staged part appears while the lock is held).
    """
    import threading

    from apps.stems import bundle_publish
    from apps.stems.bundle_lock import PUBLISH_DIR_NAME, beside_stem_root, bundle_lock

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "staged", _source(tmp_path, "a"))
    before = _snapshot(bundle)
    fresh = _publish_result(_fresh_parts(tmp_path, 5))
    staging_root = beside_stem_root(bundle.parent) / PUBLISH_DIR_NAME
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            bundle_publish.publish_stems_local(
                "staged", Path("/music/a.mp3"), fresh, bundle_publish.PRESETS["hdemucs_mmi-ov0.25"], data, requested_codec="flac"
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    with bundle_lock(bundle):
        publisher = threading.Thread(target=publish, daemon=True)
        publisher.start()
        _wait_for(
            lambda: sorted(p.name for p in staging_root.glob("staged-*/*.flac")) == sorted(f"{p}.flac" for p in PARTS),
            what="the publish to stage its parts while the lock is held",
            timeout_s=10,
        )
        assert publisher.is_alive() and _snapshot(bundle) == before, "the publish swapped into a locked bundle"
    publisher.join(timeout=60)
    assert not errors, errors
    after = _snapshot(bundle)
    assert {name: after[name] for name in after if name != "manifest.json"} == {
        f"{p}.flac": fresh["stems"][p] for p in PARTS
    }, "control: once released, the staged parts land"
    assert not any(staging_root.iterdir()), "no staging directory left"


LONG_SECONDS = 20


def _long_flac_bundle(data: Path, stable_id: str, tmp_path: Path) -> Path:
    """A real 20 s legacy FLAC bundle and its 20 s MP3 source: the encode then
    takes seconds, far longer than the milliseconds a publish takes, so the
    publish reliably lands inside it rather than after the swap."""
    frames = RATE * LONG_SECONDS
    rng = np.random.default_rng(11)
    mix = (0.3 * rng.uniform(-1.0, 1.0, size=(frames, 2))).astype(np.float32)
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    source = music / f"{stable_id}-long.mp3"
    encode_mp3(mix, RATE, source, cbr_kbps=192)
    bundle = data / "state" / "stems" / stable_id
    bundle.mkdir()
    for i, part in enumerate(PARTS):
        part_noise = (0.3 * np.random.default_rng(i).uniform(-1.0, 1.0, size=(frames, 2))).astype(np.float32)
        sf.write(bundle / f"{part}.flac", part_noise, RATE, format="FLAC", subtype="PCM_16")
    manifest = {
        "schema_version": 2,
        "stable_id": stable_id,
        "model": {"name": "hdemucs_mmi", "version": "4.0.1"},
        "source": {"path": str(source), "sha256": "0" * 64},
        "files": {part: f"{part}.flac" for part in PARTS},
        "preset": {"tag": "hdemucs_mmi-ov0.25"},
        "audio": {"sample_rate": RATE, "frame_count": frames, "channels": 2},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


def test_a_publish_mid_encode_is_not_clobbered(tmp_path: Path) -> None:
    """A stems job publishes the bundle while the pass is encoding it: the swap
    refuses, the new bundle stays exactly as published, nothing goes to backup."""
    import threading

    from apps.stems import bundle_publish

    data = _data_dir(tmp_path)
    bundle = _long_flac_bundle(data, "racing", tmp_path)
    (candidate,) = rc.plan(data)
    fresh = _publish_result(_fresh_parts(tmp_path, 3))
    published: list[dict[str, bytes]] = []

    def a_stems_job_publishes_once_the_encode_starts() -> None:
        # The new bundle's directory is made right after the locked snapshot.
        _wait_for(
            lambda: any(rc.work_parent(data / "state" / "stems").glob(".recompress-racing-*/racing")),
            what="the re-compress snapshot",
        )
        bundle_publish.publish_stems_local(
            "racing", Path("/music/a.mp3"), fresh, bundle_publish.PRESETS["hdemucs_mmi-ov0.25"], data, requested_codec="flac"
        )
        published.append(_snapshot(bundle))

    publisher = threading.Thread(target=a_stems_job_publishes_once_the_encode_starts)
    publisher.start()
    try:
        with pytest.raises(rc.BundleChangedError, match="rewritten by another writer"):
            rc.recompress_bundle(candidate, tmp_path / "backup")
    finally:
        publisher.join(timeout=60)

    assert published, "the publisher ran"
    assert _snapshot(bundle) == published[0], "the published bundle was clobbered"
    assert not (tmp_path / "backup").exists(), "nothing was moved into the backup"
    assert [p.name for p in (data / "state" / "stems").iterdir()] == ["racing"], "no work directory left"
    assert not any(rc.work_parent(data / "state" / "stems").glob(".recompress-*")), (
        "no work directory left beside the root"
    )


def test_a_swap_waits_for_a_publish_holding_the_lock(tmp_path: Path) -> None:
    """The re-compress swap takes the same lock: while a publisher holds it,
    the verified MP3 bundle is not swapped in; once released, it is."""
    import threading
    import time

    from apps.stems.bundle_lock import bundle_lock

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "held", _source(tmp_path, "a"))
    before = _snapshot(bundle)
    (candidate,) = rc.plan(data)
    outcomes: list[rc.Outcome] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            outcomes.append(rc.recompress_bundle(candidate, tmp_path / "backup"))
        except BaseException as exc:  # noqa: BLE001 - every failure is asserted on below
            errors.append(exc)

    def encoded() -> bool:
        works = list(rc.work_parent(data / "state" / "stems").glob(".recompress-held-*"))
        return bool(works) and (works[0] / "held" / "manifest.json").exists()

    worker = threading.Thread(target=run)
    worker.start()
    # The pass snapshots under the lock, then encodes without it: take the
    # lock once the snapshot is done, as a publisher arriving would.
    _wait_for(
        lambda: any(rc.work_parent(data / "state" / "stems").glob(".recompress-held-*/held")) or not worker.is_alive(),
        what="the re-compress snapshot",
    )
    with bundle_lock(bundle):
        _wait_for(lambda: encoded() or not worker.is_alive(), what="the encode to finish")
        time.sleep(1.0)
        assert worker.is_alive() and not outcomes, "the swap ran while the lock was held"
        assert _snapshot(bundle) == before
    worker.join(timeout=60)

    assert errors == []
    assert [o.stable_id for o in outcomes] == ["held"], "control: once released, it swaps"
    assert load_stem_bundle("held", roots=[data / "state" / "stems"]).media_type == "audio/mpeg"


def test_the_snapshot_waits_for_a_writer_holding_the_lock(tmp_path: Path) -> None:
    """The pass copies the bundle it will encode only under the lock, so it
    never reads a bundle a publisher is halfway through writing."""
    import threading
    import time

    from apps.stems.bundle_lock import bundle_lock

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "snap", _source(tmp_path, "a"))
    (candidate,) = rc.plan(data)
    outcomes: list[rc.Outcome] = []
    stems = data / "state" / "stems"

    with bundle_lock(bundle):
        worker = threading.Thread(target=lambda: outcomes.append(rc.recompress_bundle(candidate, tmp_path / "backup")))
        worker.start()
        _wait_for(lambda: any(rc.work_parent(stems).glob(".recompress-snap-*")), what="the re-compress work directory")
        time.sleep(1.0)  # an unlocked copy of four small parts takes milliseconds
        assert not any(rc.work_parent(stems).glob(".recompress-snap-*/snapshot/snap")), (
            "the snapshot was copied under a held lock"
        )
    worker.join(timeout=60)
    assert [o.stable_id for o in outcomes] == ["snap"], "control: once released, the pass completes"


@pytest.mark.requirement("STEM-12745")
def test_publish_into_place_alone_does_not_refresh_the_download_index(tmp_path: Path) -> None:
    """[if] publish_into_place lands without index record [then] cached index stays stale, [else stop]."""
    import json

    from apps.cloud.stem_index import MANIFEST_FILENAME, load_cached_index, save_cached_index
    from scripts.stem_bundle_worker import publish_into_place

    data = _data_dir(tmp_path)
    bundle = data / "state" / "stems" / "index"
    bundle.mkdir()
    for part in PARTS:
        (bundle / f"{part}.flac").write_bytes(b"stale")
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "stable_id": "index",
                "layout": "demucs4",
                "files": {part: f"{part}.flac" for part in PARTS},
                "codec": "flac",
            }
        )
    )
    stale_manifest_sha = "c" * 64
    save_cached_index(
        data,
        {
            "index": {
                **{f"{part}.flac": "d" * 64 for part in PARTS},
                MANIFEST_FILENAME: stale_manifest_sha,
            }
        },
    )
    tmp = data / "state" / "stems" / ".stem-index-new"
    tmp.mkdir()
    for part in PARTS:
        (tmp / f"{part}.mp3").write_bytes(b"fresh")
    (tmp / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "stable_id": "index",
                "layout": "demucs4",
                "files": {part: f"{part}.mp3" for part in PARTS},
                "codec": "mp3",
            }
        )
    )

    publish_into_place(tmp, bundle)
    cached = load_cached_index(data)["index"]
    assert cached[MANIFEST_FILENAME] == stale_manifest_sha
    assert all(name.endswith(".flac") for name in cached if name != MANIFEST_FILENAME)


def test_the_local_worker_publish_waits_for_the_bundle_lock(tmp_path: Path) -> None:
    """scripts/stem_bundle_worker.py's remove-and-rename takes the same lock."""
    import threading
    import time

    from apps.stems.bundle_lock import bundle_lock
    from scripts.stem_bundle_worker import publish_into_place

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "local", _source(tmp_path, "a"))
    before = _snapshot(bundle)
    tmp = data / "state" / "stems" / ".stem-local-new"
    tmp.mkdir()
    (tmp / "manifest.json").write_text("{}")
    done = threading.Event()

    def publish() -> None:
        publish_into_place(tmp, bundle)
        done.set()

    with bundle_lock(bundle):
        worker = threading.Thread(target=publish)
        worker.start()
        time.sleep(1.0)  # an unlocked remove-and-rename takes milliseconds
        assert not done.is_set(), "the local publish replaced a locked bundle"
        assert _snapshot(bundle) == before
    worker.join(timeout=60)
    assert done.is_set() and _snapshot(bundle) == {"manifest.json": b"{}"}, "control: it lands once released"


def _symlinked_stem_root(tmp_path: Path) -> tuple[Path, Path]:
    """``<data>/state/stems`` as a symlink to a directory on "another disk"."""
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    real = tmp_path / "disk" / "stems"
    real.mkdir(parents=True)
    (data / "state" / "stems").symlink_to(real, target_is_directory=True)
    return data, real


def test_the_local_worker_keeps_the_lock_when_the_stem_root_is_a_symlink(tmp_path: Path) -> None:
    """[if] <data>/state/stems is a symlink [then] the worker's publish still waits for the bundle lock, [else stop].

    MUTATION TARGET: resolve ``--out-dir`` in ``bundle_publish_dir``, and the
    path no longer names a data dir, so the publish takes no lock and replaces
    a bundle another writer holds (Codex on #6820)."""
    import threading
    import time

    from apps.stems.bundle_lock import bundle_lock
    from scripts.stem_bundle_worker import bundle_publish_dir, publish_into_place

    data, real = _symlinked_stem_root(tmp_path)
    bundle = _flac_bundle(data, "linked", _source(tmp_path, "a"))
    assert (real / "linked" / "manifest.json").is_file(), "control: the bundle lives on the linked disk"
    before = _snapshot(bundle)
    tmp = data / "state" / "stems" / ".stem-linked-new"
    tmp.mkdir()
    (tmp / "manifest.json").write_text("{}")
    out_dir = bundle_publish_dir(bundle)
    assert out_dir == bundle.absolute(), "the --out-dir keeps its <data>/state spelling"
    done = threading.Event()

    def publish() -> None:
        publish_into_place(tmp, out_dir)
        done.set()

    with bundle_lock(bundle):
        worker = threading.Thread(target=publish)
        worker.start()
        time.sleep(1.0)  # an unlocked remove-and-rename takes milliseconds
        assert not done.is_set(), "the local publish replaced a locked bundle"
        assert _snapshot(bundle) == before
    worker.join(timeout=60)
    assert done.is_set() and _snapshot(bundle) == {"manifest.json": b"{}"}, "control: it lands once released"


def test_a_farm_import_into_a_symlinked_stem_root_stages_and_swaps_on_its_disk(tmp_path: Path) -> None:
    """[if] <data>/state/stems is a symlink to another disk [then] staging and the swap backup sit on that disk, [else stop].

    A rename between the data dir's disk and the stem root's disk fails with
    EXDEV, so every directory a bundle is renamed out of or into must be beside
    the REAL stem root (Codex on #6820). MUTATION TARGETS: stage in the lexical
    ``stems.parent`` in ``import_tar``; put the swap backup in the lexical
    ``dest.parent.parent`` in ``swap_backup_path``."""
    import tarfile

    from apps.stems import farm_import
    from apps.stems.bundle_lock import LOCK_DIR_NAME, SWAP_DIR_NAME
    from scripts.stem_farm_runner import _bundle_tar_members

    data, real = _symlinked_stem_root(tmp_path)
    _flac_bundle(data, "farmed", _source(tmp_path, "a"))
    farm = tmp_path / "farm"
    (farm / "state" / "stems").mkdir(parents=True)
    render = _flac_bundle(farm, "farmed", _source(tmp_path, "b"))
    farmed = _snapshot(render)
    pull = tmp_path / "pull"
    pull.mkdir()
    with tarfile.open(pull / "farmed.tar", "w") as tar:
        for name in _bundle_tar_members(json.loads((render / "manifest.json").read_text())):
            tar.add(render / name, arcname=name)

    assert farm_import.import_all(pull, data / "state" / "stems") == 1
    assert _snapshot(real / "farmed") == farmed, "control: the farm bundle replaced the old one"
    disk = real.parent
    assert (disk / farm_import.STAGING_DIR_NAME).is_dir(), "staged beside the real stem root"
    assert (disk / SWAP_DIR_NAME / "stems").is_dir(), "the old bundle was set aside on the real root's disk"
    from apps.cloud.stem_cache_budget import EVICTION_CLAIM_LOCK_DIR_NAME

    assert sorted(p.name for p in (data / "state").iterdir()) == sorted(
        [LOCK_DIR_NAME, EVICTION_CLAIM_LOCK_DIR_NAME, "stems"]
    ), "nothing staged or set aside on the data dir's disk (only the shared lock files live there)"


def test_a_live_pass_over_a_symlinked_stem_root_works_and_backs_up_on_its_disk(tmp_path: Path) -> None:
    """[if] <data>/state/stems is a symlink to another disk [then] its workspace and default backup sit on that disk, [else stop].

    The swap renames the bundle into the backup and the workspace into the
    bundle; across disks either rename fails (EXDEV), and the pass's own
    device check refuses a workspace on another volume (Codex on #6820 and
    #6821). MUTATION TARGETS: ``work_parent`` from the lexical ``root.parent``;
    ``default_backup_root`` under the lexical ``<data>/state``."""
    from apps.stems.bundle_lock import LOCK_DIR_NAME

    data, real = _symlinked_stem_root(tmp_path)
    bundle = _flac_bundle(data, "linked", _source(tmp_path, "a"))
    old = _snapshot(bundle)
    disk = real.parent

    assert rc.main(["--data-dir", str(data), "--live", "--no-jobs-db-ok"]) == 0

    loaded = load_stem_bundle("linked", roots=[real])
    assert loaded.media_type == "audio/mpeg", "control: it was re-compressed"
    (stamp,) = (disk / rc.BACKUP_DIR_NAME / real.name).iterdir()
    assert _snapshot(stamp / "stems" / "linked") == old, "the old bundle is kept on the stem root's disk"
    assert (disk / rc.WORK_DIR_NAME).is_dir(), "the workspace was made on the stem root's disk"
    from apps.cloud.stem_cache_budget import EVICTION_CLAIM_LOCK_DIR_NAME

    locks = {LOCK_DIR_NAME, EVICTION_CLAIM_LOCK_DIR_NAME}
    # 1355bbf640 (Wed 11 Nov 2026) records the download index cache under the data dir after a live swap.
    allowed = {rc.SWAP_JOURNAL_DIR_NAME, "stems", INDEX_CACHE_FILENAME}
    on_data_disk = {p.name for p in (data / "state").iterdir()} - locks - allowed
    assert not on_data_disk, f"nothing but the lock files and swap journal on the data dir's disk: {on_data_disk}"


def test_two_data_dirs_on_sibling_stem_roots_get_their_own_default_backups(tmp_path: Path) -> None:
    """[if] two data dirs' stem roots link to sibling directories on one disk [then] their default backups never share a path, [else stop].

    Both roots are ``state/stems`` in their data dirs; two passes started in
    the same second would otherwise move bundles of one stable id to one
    backup path (Codex on #6820). MUTATION TARGET: leave the resolved root's
    name out of ``default_backup_root``.
    """
    disk = tmp_path / "disk"
    roots = []
    for name in ("A", "B"):
        (disk / name).mkdir(parents=True)
        state = tmp_path / f"data-{name}" / "state"
        state.mkdir(parents=True)
        (state / "stems").symlink_to(disk / name, target_is_directory=True)
        roots.append(state / "stems")
    a, b = (rc.default_backup_root(root, "20261022T143000") for root in roots)
    assert a != b
    assert a.parent.parent == b.parent.parent == disk.resolve() / rc.BACKUP_DIR_NAME, "both on the stem roots' disk"


def test_the_farm_local_publish_waits_for_the_bundle_lock(tmp_path: Path) -> None:
    """scripts/modal_vocal_farm.py --stems-dest local writes under the same lock
    (Codex P1 on #6820). The farm's REAL ``_publish_stems_local`` is compiled
    from its source, as tests/stems/test_bundle_publish_mirror.py does, so no
    modal import is needed."""
    import threading
    import time

    from apps.stems import bundle_publish
    from apps.stems.bundle_lock import bundle_lock
    from tests.stems.test_bundle_publish_mirror import _farm_function, _farm_namespace, _farm_tree

    tree = _farm_tree()
    namespace = _farm_namespace(tree)
    _farm_function(tree, "_stem_manifest", namespace)
    farm_publish = _farm_function(tree, "_publish_stems_local", namespace)

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "farm", _source(tmp_path, "a"))
    before = _snapshot(bundle)
    fresh = _publish_result(_fresh_parts(tmp_path, 5))
    done = threading.Event()

    def publish() -> None:
        farm_publish(
            "farm", Path("/music/a.mp3"), fresh, bundle_publish.PRESETS["hdemucs_mmi-ov0.25"], data,
            requested_codec="flac",
        )
        done.set()

    with bundle_lock(bundle):
        worker = threading.Thread(target=publish)
        worker.start()
        time.sleep(1.0)  # an unlocked publish of four small parts takes milliseconds
        assert not done.is_set(), "the farm publish wrote into a locked bundle"
        assert _snapshot(bundle) == before
    worker.join(timeout=60)
    assert done.is_set() and _snapshot(bundle) != before, "control: it lands once released"


def test_a_deliberate_bundle_that_replaced_the_candidate_is_left_alone(tmp_path: Path) -> None:
    """A job publishes a deliberately lossless bundle after plan() and before the
    snapshot: the pass re-classifies the locked copy and leaves it untouched
    (Codex P1 on #6820)."""
    import shutil

    data = _data_dir(tmp_path)
    source = _source(tmp_path, "a")
    stems = data / "state" / "stems"
    _flac_bundle(data, "swapped", source)
    (candidate,) = rc.plan(data)
    assert candidate.verdict == rc.ELIGIBLE, "control: the plan saw a legacy bundle"

    shutil.rmtree(stems / "swapped")
    _bundle_with(
        data,
        "swapped",
        source,
        {**_V3, "codec": "flac", "size_policy": _FLAC_POLICY, "preset": {"tag": "htdemucs_ft-ov0.25"}},
    )
    published = _snapshot(stems / "swapped")

    with pytest.raises(rc.BundleChangedError, match="no longer eligible"):
        rc.recompress_bundle(candidate, tmp_path / "backup")

    assert _snapshot(stems / "swapped") == published
    assert not (tmp_path / "backup").exists()
    assert [p.name for p in stems.iterdir()] == ["swapped"], "no work directory left"
    assert not any(rc.work_parent(stems).glob(".recompress-*")), "no work directory left beside the root"


def test_an_unchanged_legacy_candidate_is_still_converted(tmp_path: Path) -> None:
    """The overshoot control for the re-classification: nothing changed since
    the plan, so the bundle is converted."""
    data = _data_dir(tmp_path)
    _flac_bundle(data, "steady", _source(tmp_path, "a"))
    (candidate,) = rc.plan(data)

    outcome = rc.recompress_bundle(candidate, tmp_path / "backup")

    assert outcome.stable_id == "steady"
    assert load_stem_bundle("steady", roots=[data / "state" / "stems"]).media_type == "audio/mpeg"


def _relink(data: Path, stable_id: str, path: Path) -> None:
    db = sqlite3.connect(data / "state" / "state.db")
    db.execute("UPDATE tracks SET file_path = ? WHERE stable_id = ?", (str(path), stable_id))
    db.commit()
    db.close()


def _planned_from_library(tmp_path: Path, stable_id: str) -> tuple[Path, Path, rc.Candidate]:
    """A FLAC bundle whose manifest names one MP3 and whose library row names another."""
    data = _data_dir(tmp_path)
    recorded = _source(tmp_path, "recorded")
    library = _source(tmp_path, "library", kbps=128)
    _flac_bundle(data, stable_id, recorded)
    _state_db(data, [(stable_id, str(library), None)])
    (candidate,) = rc.plan(data)
    assert (candidate.verdict, candidate.source_path) == (rc.ELIGIBLE, library), "control: the library path won"
    return data, library, candidate


@pytest.mark.parametrize(
    ("relinked_to", "refusal"),
    [(None, "no longer eligible .*lossless source"), (192, "library source changed")],
    ids=["to-a-lossless-file", "to-another-mp3"],
)
def test_a_track_relinked_since_the_plan_is_left_alone(
    tmp_path: Path, relinked_to: int | None, refusal: str
) -> None:
    """state.db relinks the track after plan(): the pass re-reads the library row
    under the lock and refuses, instead of encoding against the planned path
    (Codex P2 on #6820)."""
    data, _library, candidate = _planned_from_library(tmp_path, "relinked")
    _relink(data, "relinked", _source(tmp_path, "relinked-to", kbps=relinked_to))
    bundle = data / "state" / "stems" / "relinked"
    before = _snapshot(bundle)

    with pytest.raises(rc.BundleChangedError, match=refusal):
        rc.recompress_bundle(candidate, tmp_path / "backup")

    assert _snapshot(bundle) == before
    assert not (tmp_path / "backup").exists()


def test_an_unchanged_library_path_is_converted_from_that_path(tmp_path: Path) -> None:
    """The overshoot control for the relink check: nothing relinked, so the
    bundle converts, and its size policy is measured against the library's
    file, not the manifest's recorded one."""
    data, library, candidate = _planned_from_library(tmp_path, "steady-lib")

    outcome = rc.recompress_bundle(candidate, tmp_path / "backup")

    assert outcome.source_bytes == library.stat().st_size
    manifest = json.loads((data / "state" / "stems" / "steady-lib" / "manifest.json").read_text())
    assert manifest["codec"] == "mp3" and manifest["size_policy"]["source_bytes"] == library.stat().st_size


@pytest.mark.parametrize(
    ("relinked_to", "refusal"),
    [(None, "no longer eligible .*lossless source"), (192, "library source changed")],
    ids=["to-a-lossless-file", "to-another-mp3"],
)
def test_a_relink_while_the_parts_encode_stops_the_swap(
    tmp_path: Path, relinked_to: int | None, refusal: str
) -> None:
    """state.db relinks the track AFTER the snapshot, while the parts encode:
    the swap re-reads the library row under its lock and refuses, instead of
    swapping in parts sized against the old source (Codex P2 on #6821)."""
    data, _library, candidate = _planned_from_library(tmp_path, "mid-relink")
    bundle = data / "state" / "stems" / "mid-relink"
    before = _snapshot(bundle)
    target = _source(tmp_path, "mid-relink-to", kbps=relinked_to)

    with pytest.raises(rc.BundleChangedError, match=refusal):
        rc.recompress_bundle(candidate, tmp_path / "backup", before_swap=lambda: _relink(data, "mid-relink", target))

    assert _snapshot(bundle) == before
    assert not (tmp_path / "backup").exists()


def test_a_source_rewritten_while_the_parts_encode_stops_the_swap(tmp_path: Path) -> None:
    """Same path in state.db, but the file at it was replaced mid-encode: the
    size the ladder was measured against no longer holds, so nothing swaps."""
    data, library, candidate = _planned_from_library(tmp_path, "mid-rewrite")
    bundle = data / "state" / "stems" / "mid-rewrite"
    before = _snapshot(bundle)

    def rewrite_source() -> None:
        library.write_bytes(library.read_bytes() + b"\0" * 4096)

    with pytest.raises(rc.BundleChangedError, match="changed while this pass encoded it"):
        rc.recompress_bundle(candidate, tmp_path / "backup", before_swap=rewrite_source)

    assert _snapshot(bundle) == before
    assert not (tmp_path / "backup").exists()


def test_control_an_unrelated_write_mid_encode_still_swaps(tmp_path: Path) -> None:
    """The overshoot control: a state.db write that leaves this track's path
    and source untouched does not stop the swap."""
    data, library, candidate = _planned_from_library(tmp_path, "mid-steady")

    outcome = rc.recompress_bundle(
        candidate, tmp_path / "backup", before_swap=lambda: _relink(data, "some-other-track", library)
    )

    assert outcome.source_bytes == library.stat().st_size
    assert load_stem_bundle("mid-steady", roots=[data / "state" / "stems"]).media_type == "audio/mpeg"


def test_a_library_row_whose_file_is_missing_does_not_fall_back_to_the_manifest(tmp_path: Path) -> None:
    """state.db names the track's current file and it is gone: the bundle is
    refused as having no source, not recompressed against the older file the
    manifest still names, which may be another recording (Sol on #6820)."""
    data = _data_dir(tmp_path)
    recorded = _source(tmp_path, "recorded")  # still on disk
    _flac_bundle(data, "missing-now", recorded)
    _state_db(data, [("missing-now", str(tmp_path / "music" / "relinked-gone.mp3"), None)])

    (candidate,) = rc.plan(data)

    assert candidate.verdict == rc.SKIP_NO_SOURCE
    assert "relinked-gone.mp3" in candidate.detail


def test_control_with_no_library_row_the_manifest_path_is_still_used(tmp_path: Path) -> None:
    """The overshoot control: a track state.db does not know keeps the manifest's source."""
    data = _data_dir(tmp_path)
    recorded = _source(tmp_path, "recorded")
    _flac_bundle(data, "unlisted", recorded)
    _state_db(data, [])

    (candidate,) = rc.plan(data)

    assert (candidate.verdict, candidate.source_path) == (rc.ELIGIBLE, recorded)


def test_a_machine_local_track_location_beats_a_foreign_tracks_file_path(tmp_path: Path) -> None:
    """[if] tracks.file_path names another machine's path but this machine's track_locations row plays [then] that row is the source, [else stop].

    A CloudSync machine keeps the other machine's path in the legacy column
    and its own in ``track_locations``; reading only the column called every
    such bundle "no source" (Codex on #6820). The state.db is written by the
    production ``StateWriter``. Control: with the location row gone, the
    foreign path is reported missing, never replaced by the manifest's.

    MUTATION TARGET: read only ``tracks.file_path`` in ``library_paths``.
    """
    from apps.shared.state import db as state_db
    from apps.shared.state.writer import StateWriter

    data = _data_dir(tmp_path)
    local = _source(tmp_path, "local-copy")
    recorded = _source(tmp_path, "recorded")
    _flac_bundle(data, "cloudsync-track", recorded)
    conn = state_db.open_rw(data / "state" / "state.db")
    writer = StateWriter(conn, actor="unit-test")
    try:
        writer.upsert_track(
            stable_id="cloudsync-track", stable_id_tier="inferred", title="Demo",
            artists=["X"], album=None, isrc=None, duration_ms=180_000, file_path=str(local),
        )
        conn.execute(
            "UPDATE tracks SET file_path = ? WHERE stable_id = ?",
            ("/Users/dev/Music/on-the-other-mac.mp3", "cloudsync-track"),
        )
        conn.commit()
    finally:
        writer.close()
        conn.close()

    (candidate,) = rc.plan(data)
    assert (candidate.verdict, candidate.source_path) == (rc.ELIGIBLE, local)

    conn = state_db.open_rw(data / "state" / "state.db")
    try:
        conn.execute("DELETE FROM track_locations WHERE stable_id = ?", ("cloudsync-track",))
        conn.commit()
    finally:
        conn.close()
    (candidate,) = rc.plan(data)
    assert candidate.verdict != rc.ELIGIBLE
    assert candidate.source_path != recorded


def test_a_source_path_this_machine_cannot_stat_is_no_source_not_a_crash(tmp_path: Path) -> None:
    """[if] a recorded source path cannot be stat-ed here [then] the bundle has no source and the plan goes on, [else stop].

    Another machine's home this one may not enter raised ``PermissionError``
    out of ``Path.is_file`` and aborted the whole plan on a CI runner with an
    unreadable ``/Users``. A name too long for the filesystem raises the same
    way for every user, root included, so it stands in for that class here,
    once as the library's path and once as the manifest's. Control: the same
    bundle with a reachable source is eligible.

    MUTATION TARGET: call ``path.is_file()`` directly in ``_source_for``.
    """
    from apps.shared.state import db as state_db
    from apps.shared.state.writer import StateWriter

    unreachable = Path("/" + "x" * 300) / "track.mp3"
    data = _data_dir(tmp_path)
    _flac_bundle(data, "manifest-only", unreachable)
    (candidate,) = rc.plan(data)
    assert candidate.verdict != rc.ELIGIBLE
    assert candidate.source_path is None

    reachable = _source(tmp_path, "reachable")
    _flac_bundle(data, "library-row", reachable)
    by_id = {c.stable_id: c for c in rc.plan(data)}
    assert (by_id["library-row"].verdict, by_id["library-row"].source_path) == (rc.ELIGIBLE, reachable), (
        "control: a reachable manifest source is eligible"
    )

    conn = state_db.open_rw(data / "state" / "state.db")
    writer = StateWriter(conn, actor="unit-test")
    try:
        writer.upsert_track(
            stable_id="library-row", stable_id_tier="inferred", title="Demo",
            artists=["X"], album=None, isrc=None, duration_ms=180_000, file_path=str(unreachable),
        )
    finally:
        writer.close()
        conn.close()
    by_id = {c.stable_id: c for c in rc.plan(data)}
    assert by_id["library-row"].verdict != rc.ELIGIBLE
    assert by_id["library-row"].source_path is None


def _short_source(tmp_path: Path, name: str, seconds: float) -> Path:
    mix = (sum(_noise(i) for i in range(4)) / 4)[: int(RATE * seconds)]
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    path = music / f"{name}.mp3"
    encode_mp3(mix, RATE, path, cbr_kbps=192)
    return path


def test_a_source_of_another_length_is_refused(tmp_path: Path) -> None:
    """The source on disk is a different-length recording than the stems: its
    bitrate would be computed over the wrong duration, so the bundle is left
    alone (Sol on #6820)."""
    data = _data_dir(tmp_path)
    _flac_bundle(data, "other-cut", _short_source(tmp_path, "other-cut", SECONDS - 1.5))

    (candidate,) = rc.plan(data)

    assert candidate.verdict == rc.SKIP_SOURCE_DURATION
    assert "vs stems 3.000s" in candidate.detail


def test_control_a_source_within_the_drift_tolerance_is_eligible(tmp_path: Path) -> None:
    """Encoder padding and a few hundred ms of drift are the same recording."""
    data = _data_dir(tmp_path)
    _flac_bundle(data, "near", _short_source(tmp_path, "near", SECONDS - 0.2))

    (candidate,) = rc.plan(data)

    assert candidate.verdict == rc.ELIGIBLE


def test_the_size_ladder_uses_the_source_file_s_own_duration(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    source = _source(tmp_path, "timed")
    _flac_bundle(data, "timed", source)
    (candidate,) = rc.plan(data)

    rc.recompress_bundle(candidate, tmp_path / "backup")

    manifest = json.loads((data / "state" / "stems" / "timed" / "manifest.json").read_text())
    duration = rc._source_duration_s(source)
    assert duration is not None, "control: the fixture source has a readable duration"
    expected = round(rc.pol.effective_bitrate_kbps(source.stat().st_size, duration), 2)
    assert manifest["size_policy"]["source_kbps"] == expected


def _record_source_sha256(bundle: Path, source: Path) -> None:
    import hashlib

    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["source"]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    (bundle / "manifest.json").write_text(json.dumps(manifest))


def _overwrite_with_another_recording(source: Path, seed: int) -> None:
    """Same path, same length, different audio: what a re-rip or a tag tool's
    rewrite with another master leaves behind."""
    mix = sum(_noise(seed + i) for i in range(4)) / 4
    encode_mp3(mix, RATE, source, cbr_kbps=192)


def test_a_source_overwritten_with_another_recording_is_refused(tmp_path: Path) -> None:
    """[if] the file at the source path is no longer the bytes the stems
    recorded [then] the bundle is left as it is, [else stop] (Codex on #6820).

    MUTATION TARGET: drop the recorded-hash comparison and the bundle is
    re-encoded at the replacement's bitrate under the old provenance.
    """
    data = _data_dir(tmp_path)
    source = _source(tmp_path, "rerip")
    bundle = _flac_bundle(data, "rerip", source)
    _record_source_sha256(bundle, source)
    (candidate,) = rc.plan(data)
    _overwrite_with_another_recording(source, seed=40)
    before = _snapshot(bundle)

    with pytest.raises(rc.BundleChangedError, match="not the source these stems were separated from"):
        rc.recompress_bundle(candidate, tmp_path / "backup")

    assert _snapshot(bundle) == before
    assert not (tmp_path / "backup").exists()


def test_control_a_source_matching_its_recorded_hash_is_converted(tmp_path: Path) -> None:
    """Overshoot control: a recorded hash that matches does not block the pass."""
    data = _data_dir(tmp_path)
    source = _source(tmp_path, "same")
    bundle = _flac_bundle(data, "same", source)
    _record_source_sha256(bundle, source)
    (candidate,) = rc.plan(data)

    rc.recompress_bundle(candidate, tmp_path / "backup")

    assert json.loads((bundle / "manifest.json").read_text())["files"]["vocals"].endswith(".mp3")


def test_a_source_rewritten_while_the_pass_encodes_is_not_swapped_in(tmp_path: Path) -> None:
    """[if] the source changes between the hash the pass took and the swap
    [then] the swap refuses, [else stop]: the bitrate was taken from bytes
    that are no longer there (Codex on #6820).

    MUTATION TARGET: drop the re-hash before the swap and the bundle is
    replaced from a source that changed under the encode.
    """
    import threading

    data = _data_dir(tmp_path)
    bundle = _long_flac_bundle(data, "moving", tmp_path)
    source = Path(json.loads((bundle / "manifest.json").read_text())["source"]["path"])
    (candidate,) = rc.plan(data)
    before = _snapshot(bundle)

    def a_tag_tool_rewrites_the_source_once_the_encode_starts() -> None:
        _wait_for(
            lambda: any(rc.work_parent(data / "state" / "stems").glob(".recompress-moving-*/moving")),
            what="the re-compress work directory",
        )
        with source.open("ab") as handle:
            handle.write(b"\0" * 128)

    writer = threading.Thread(target=a_tag_tool_rewrites_the_source_once_the_encode_starts)
    writer.start()
    try:
        with pytest.raises(rc.BundleChangedError, match="changed while this pass encoded it"):
            rc.recompress_bundle(candidate, tmp_path / "backup")
    finally:
        writer.join(timeout=60)

    assert _snapshot(bundle) == before
    assert not (tmp_path / "backup").exists()


def test_a_recompressed_manifest_hashes_every_part_for_stems_hydrate(tmp_path: Path) -> None:
    """[if] a bundle is recompressed [then] files_sha256 hashes every MP3 part and stems hydrate accepts it, [else stop].

    ``apps.lyrics.stems_sync.hydrate`` fetches parts by ``files_sha256`` and
    refuses a manifest missing an entry for any part (Codex on #6820). Its
    dry run makes that check and fetches nothing."""
    import hashlib

    from apps.lyrics import stems_sync

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "mp3src", _source(tmp_path, "a"))
    assert rc.main(["--data-dir", str(data), "--live", "--backup-dir", str(tmp_path / "backup")]) == 0

    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["files_sha256"] == {
        part: hashlib.sha256((bundle / rel).read_bytes()).hexdigest() for part, rel in manifest["files"].items()
    }
    assert stems_sync.hydrate("mp3src", manifest_path=bundle / "manifest.json", data_dir=data, dry_run=True) == 0


def test_the_new_manifest_names_the_source_it_was_encoded_against(tmp_path: Path) -> None:
    """[if] a bundle is recompressed from a relinked source [then] its manifest records that path and hash, [else stop].

    The old manifest named another file and the legacy all-zero hash; keeping
    either would point the next source-identity check at the wrong file or at
    nothing (Codex on #6820). The model block, which has no such problem, is
    still carried over."""
    import hashlib

    data, library, candidate = _planned_from_library(tmp_path, "renamed")
    rc.recompress_bundle(candidate, tmp_path / "backup")

    manifest = json.loads((data / "state" / "stems" / "renamed" / "manifest.json").read_text())
    assert manifest["source"]["path"] == str(library)
    assert manifest["source"]["sha256"] == hashlib.sha256(library.read_bytes()).hexdigest()
    assert manifest["model"]["name"] == "hdemucs_mmi"


def test_the_gce_farm_import_waits_for_the_bundle_lock(tmp_path: Path) -> None:
    """[if] another writer holds a bundle's lock [then] the GCE farm import waits, then replaces it whole, [else stop].

    scripts/stem_farm_watch_pull_down.sh unpacked its tars straight into the
    live bundle with no lock, so a re-compress pass could move a half-extracted
    bundle (Codex P1 on #6821). It now calls apps.stems.farm_import, which
    unpacks beside the stem root and swaps in under the lock."""
    import tarfile
    import threading
    import time

    from apps.stems import farm_import
    from apps.stems.bundle_lock import bundle_lock
    from scripts.stem_farm_runner import _bundle_tar_members

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "farmed", _source(tmp_path, "a"))
    before = _snapshot(bundle)
    # The farm's own render of the same id, packed the way the GCE runner
    # packs it (scripts/stem_farm_runner.py), against a different source so
    # its manifest tells it apart from the live bundle.
    farm = tmp_path / "farm"
    (farm / "state" / "stems").mkdir(parents=True)
    render = _flac_bundle(farm, "farmed", _source(tmp_path, "b"))
    farmed = _snapshot(render)
    assert farmed != before, "control: the farm render differs from the bundle it replaces"
    pull = tmp_path / "pull"
    pull.mkdir()
    with tarfile.open(pull / "farmed.tar", "w") as tar:
        for name in _bundle_tar_members(json.loads((render / "manifest.json").read_text())):
            tar.add(render / name, arcname=name)
    done = threading.Event()

    def run_import() -> None:
        farm_import.import_all(pull, data / "state" / "stems")
        done.set()

    with bundle_lock(bundle):
        worker = threading.Thread(target=run_import)
        worker.start()
        time.sleep(1.0)  # an unlocked extract takes milliseconds
        assert not done.is_set(), "the farm import wrote into a locked bundle"
        assert _snapshot(bundle) == before
    worker.join(timeout=60)
    assert done.is_set(), "control: it lands once released"
    assert _snapshot(bundle) == farmed, "the farm bundle replaced the old one whole"
    loaded = load_stem_bundle("farmed", roots=[data / "state" / "stems"])
    assert loaded.media_type == "audio/flac", "and it loads as a valid stem bundle"
    assert not any((data / "state" / farm_import.STAGING_DIR_NAME).iterdir()), "no staging left behind"
    from apps.stems.bundle_lock import swap_backup_path

    assert not swap_backup_path(bundle).exists(), "no swap backup left behind"


def test_an_incomplete_farm_tar_never_replaces_a_working_bundle(tmp_path: Path) -> None:
    """[if] a farm tar is a valid archive missing one part [then] the import refuses it and the old bundle stays, [else stop].

    The GCE runner writes its outbox tar in place, so a failure after some
    members leaves a tar that opens and extracts cleanly with parts missing.

    MUTATION TARGET: drop the strict read of the staged bundle, and the
    partial bundle replaces the working one (Codex P1 on #6821).
    """
    import tarfile

    from apps.stems import farm_import
    from apps.stems.artifacts import StemArtifactError
    from scripts.stem_farm_runner import _bundle_tar_members

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "partial", _source(tmp_path, "a"))
    before = _snapshot(bundle)
    farm = tmp_path / "farm"
    (farm / "state" / "stems").mkdir(parents=True)
    render = _flac_bundle(farm, "partial", _source(tmp_path, "b"))
    members = _bundle_tar_members(json.loads((render / "manifest.json").read_text()))
    dropped = next(name for name in members if name != "manifest.json")
    pull = tmp_path / "pull"
    pull.mkdir()
    with tarfile.open(pull / "partial.tar", "w") as tar:
        for name in members:
            if name != dropped:
                tar.add(render / name, arcname=name)

    with pytest.raises(StemArtifactError):
        farm_import.import_all(pull, data / "state" / "stems")

    assert _snapshot(bundle) == before
    assert not any((data / "state" / farm_import.STAGING_DIR_NAME).iterdir()), "no staging left behind"


def test_a_failed_bundle_swap_keeps_the_old_bundle(tmp_path: Path) -> None:
    """[if] renaming the new bundle into place fails [then] the old bundle is still there, whole, [else stop].

    The failure is the OS's own: the new directory was never written, so its
    rename raises FileNotFoundError after the old bundle has moved aside.

    MUTATION TARGET: remove the old bundle before the rename, and one failed
    rename (a transient filesystem error, a Windows file lock) leaves the track
    with no bundle at all (Codex on #6820).
    """
    from apps.stems.bundle_lock import replace_bundle_dir, swap_backup_path

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "kept", _source(tmp_path, "a"))
    before = _snapshot(bundle)

    with pytest.raises(FileNotFoundError):
        replace_bundle_dir(tmp_path / "never-written", bundle)

    assert _snapshot(bundle) == before
    assert not swap_backup_path(bundle).exists(), "no backup left behind"


def test_a_backup_orphaned_mid_swap_goes_back_before_the_next_swap(tmp_path: Path) -> None:
    """[if] a writer died between its two renames, leaving only the backup [then] the next swap restores it first, [else stop].

    MUTATION TARGET: delete any backup found on entry, and the only good copy
    of the bundle is gone before the next swap has landed (Codex on #6820).
    """
    import os

    from apps.stems.bundle_lock import replace_bundle_dir, swap_backup_path

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "orphaned", _source(tmp_path, "a"))
    before = _snapshot(bundle)
    backup = swap_backup_path(bundle)
    backup.parent.mkdir(parents=True)
    os.rename(bundle, backup)  # exactly where a writer killed after its first rename leaves it

    with pytest.raises(FileNotFoundError):
        replace_bundle_dir(tmp_path / "never-written", bundle)

    assert _snapshot(bundle) == before
    assert not backup.exists()


def test_control_a_backup_beside_a_live_bundle_never_replaces_it(tmp_path: Path) -> None:
    """[if] a stale backup sits beside a bundle already in place and the next swap fails [then] the live bundle stays, [else stop].

    A swap that landed but died before its cleanup leaves that backup; the
    bundle at ``dest`` is the newer one.

    MUTATION TARGET: restore any backup found on entry, and the stale old
    bundle overwrites the one the last swap published.
    """
    import shutil

    from apps.stems.bundle_lock import replace_bundle_dir, swap_backup_path

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "landed", _source(tmp_path, "a"))
    live = _snapshot(bundle)
    backup = swap_backup_path(bundle)
    backup.parent.mkdir(parents=True)
    shutil.copytree(bundle, backup)
    (backup / "manifest.json").write_text("{}")  # the stale old bundle, not the live one

    with pytest.raises(FileNotFoundError):
        replace_bundle_dir(tmp_path / "never-written", bundle)

    assert _snapshot(bundle) == live
    assert not backup.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="read-only directory modes are POSIX; Windows refuses the move through an open handle instead",
)
@pytest.mark.skipif(
    os.name != "nt" and os.geteuid() == 0,
    reason="root bypasses directory permissions, so a read-only backup cannot refuse its move under root",
)
def test_a_stale_backup_that_cannot_be_cleared_stops_the_publish_before_anything_moves(tmp_path: Path) -> None:
    """[if] a landed swap's backup can be neither moved nor removed [then] the next publish raises before any rename, [else stop].

    Both bundles and the backup stay where they were.

    The publish went on into ``dest.rename(backup)``, which failed on the
    occupied slot, and its rollback then failed too, hiding the cause (Codex
    on #6820). Read-only directories make the move and the delete fail for
    real, as an open handle does on Windows. Control: once the backup can
    go, the same publish lands.

    MUTATION TARGET: ignore ``discard_landed_swap_backup``'s result in
    ``replace_bundle_dir``.
    """
    import shutil

    from apps.stems.bundle_lock import SwapBackupStuckError, replace_bundle_dir, swap_backup_path

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "stuck", _source(tmp_path, "a"))
    live = _snapshot(bundle)
    backup = swap_backup_path(bundle)
    backup.parent.mkdir(parents=True)
    shutil.copytree(bundle, backup)
    (backup / "manifest.json").write_text("{}")  # the stale old bundle
    new_dir = tmp_path / "new" / "stuck"
    shutil.copytree(bundle, new_dir)
    (new_dir / "manifest.json").write_text('{"new": true}')
    fresh = _snapshot(new_dir)
    backup.chmod(0o500)
    backup.parent.chmod(0o500)
    try:
        with pytest.raises(SwapBackupStuckError) as raised:
            replace_bundle_dir(new_dir, bundle)
        assert raised.value.backup == backup
        assert _snapshot(bundle) == live
        assert _snapshot(new_dir) == fresh
        assert (backup / "manifest.json").read_text() == "{}"
    finally:
        backup.parent.chmod(0o700)
        backup.chmod(0o700)

    replace_bundle_dir(new_dir, bundle)
    assert _snapshot(bundle) == fresh, "control: a removable backup lets the publish land"
    assert not backup.exists()


def test_the_watch_script_imports_through_the_locked_importer() -> None:
    """The wiring: the shell script calls apps.stems.farm_import and no longer extracts on its own."""
    script = (Path(__file__).resolve().parents[2] / "scripts" / "stem_farm_watch_pull_down.sh").read_text()
    assert "python -m apps.stems.farm_import" in script
    assert "extractall" not in script


def _kill_between_the_two_renames(data: Path, stable_id: str, backup_root: Path) -> tuple[Path, Path, dict[str, bytes]]:
    """Leave exactly what a pass killed after moving the old bundle aside leaves: a journal and only the backup."""
    bundle = data / "state" / "stems" / stable_id
    before = _snapshot(bundle)
    backup = backup_root / "stems" / stable_id
    backup.parent.mkdir(parents=True)
    rc._write_swap_journal(bundle, backup)
    bundle.rename(backup)
    return bundle, backup, before


def test_a_pass_killed_mid_swap_is_repaired_before_the_next_plan(tmp_path: Path) -> None:
    """[if] a pass died between moving the old bundle out and the new one in [then] the next live run restores it, then converts it, [else stop].

    MUTATION TARGET: skip the recovery, and plan() never sees the bundle: the
    track keeps no live bundle, its only copy stranded in the backup (Codex on
    #6820).
    """
    data = _data_dir(tmp_path)
    _flac_bundle(data, "killed", _source(tmp_path, "a"))
    _bundle, backup, before = _kill_between_the_two_renames(data, "killed", tmp_path / "old-backup")
    assert rc.orphaned_swaps(data), "the journal names the interrupted swap"

    assert rc.main(["--data-dir", str(data), "--live", "--backup-dir", str(tmp_path / "backup")]) == 0

    assert load_stem_bundle("killed", roots=[data / "state" / "stems"]).media_type == "audio/mpeg"
    assert _snapshot(tmp_path / "backup" / "stems" / "killed") == before
    assert not backup.exists()
    assert rc.orphaned_swaps(data) == []


def test_a_dry_run_names_an_interrupted_swap_and_changes_nothing(tmp_path: Path) -> None:
    """[if] a swap was interrupted and the run is a dry run [then] it is named and nothing moves, [else stop]."""
    data = _data_dir(tmp_path)
    _flac_bundle(data, "killed", _source(tmp_path, "a"))
    bundle, backup, before = _kill_between_the_two_renames(data, "killed", tmp_path / "old-backup")

    assert rc.main(["--data-dir", str(data)]) == 0

    assert not bundle.exists()
    assert _snapshot(backup) == before
    assert len(rc.orphaned_swaps(data)) == 1


def test_control_a_swap_that_landed_keeps_the_new_bundle(tmp_path: Path) -> None:
    """[if] a journal outlived a swap that landed [then] recovery keeps the new bundle and the backup both, [else stop].

    MUTATION TARGET: restore the backup whenever a journal is found, and the
    old lossless bundle overwrites the one the pass published.
    """
    data = _data_dir(tmp_path)
    _flac_bundle(data, "landed", _source(tmp_path, "a"))
    bundle, backup, before = _kill_between_the_two_renames(data, "landed", tmp_path / "old-backup")
    shutil.copytree(backup, bundle)
    (bundle / "manifest.json").write_text('{"published": "after the swap"}')
    live = _snapshot(bundle)

    assert rc.recover_orphaned_swaps(data) == ["landed: swap had landed; nothing to restore"]

    assert _snapshot(bundle) == live
    assert _snapshot(backup) == before
    assert rc.orphaned_swaps(data) == []


def test_recovery_waits_for_a_swap_still_under_way_and_reads_its_journal_after(tmp_path: Path) -> None:
    """[if] another pass holds the bundle lock mid-swap [then] recovery waits, and its finished swap is no error, [else stop].

    MUTATION TARGET: read the journal before taking the bundle lock, and the
    entry the other pass removes while recovery waits raises FileNotFoundError
    (Codex on #6820).
    """
    import threading

    from apps.stems.bundle_lock import bundle_lock

    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "busy", _source(tmp_path, "a"))
    journal = rc._write_swap_journal(bundle, tmp_path / "backup" / "stems" / "busy")
    answer: list[list[str]] = []
    errors: list[BaseException] = []

    def _recover() -> None:
        try:
            answer.append(rc.recover_orphaned_swaps(data))
        except BaseException as exc:  # noqa: BLE001 - reported by the assert below
            errors.append(exc)

    with bundle_lock(bundle):
        worker = threading.Thread(target=_recover)
        worker.start()
        worker.join(0.5)
        assert worker.is_alive(), "recovery must wait for the pass holding the lock"
        journal.unlink()  # the pass finishes its swap and removes its own entry
    worker.join(10)

    assert errors == []
    assert answer == [["busy: the pass that wrote this entry finished it"]]
    assert load_stem_bundle("busy", roots=[data / "state" / "stems"]).media_type != "audio/mpeg", (
        "control: the bundle the other pass left is untouched"
    )


def test_recovery_waits_for_a_deck_read_restoring_the_same_swap(tmp_path: Path) -> None:
    """[if] a deck read holds the read barrier while restoring a killed swap [then] recovery waits for it and finds the work done, [else stop].

    The read path (``restore_orphaned_recompress_swap``) holds only the read
    barrier, so recovery holding only the bundle lock could pass the same
    checks and race it to the rename, one side then raising
    FileNotFoundError (Codex on #6820). Recovery now takes the barrier
    after the bundle lock, as a live swap does. Control: the read's own
    restore is what the bundle shows afterwards.

    MUTATION TARGET: drop ``readers_held_off`` from recovery.
    """
    import threading

    from apps.cloud.stem_cache_budget import read_barrier

    data = _data_dir(tmp_path)
    _flac_bundle(data, "raced", _source(tmp_path, "a"))
    bundle, backup, before = _kill_between_the_two_renames(data, "raced", tmp_path / "old-backup")
    answer: list[list[str]] = []
    errors: list[BaseException] = []

    def _recover() -> None:
        try:
            answer.append(rc.recover_orphaned_swaps(data))
        except BaseException as exc:  # noqa: BLE001 - reported by the assert below
            errors.append(exc)

    with read_barrier(data, "raced"):
        worker = threading.Thread(target=_recover)
        worker.start()
        worker.join(0.5)
        assert worker.is_alive(), "recovery must wait for the read holding the barrier"
        assert rc.restore_orphaned_recompress_swap(bundle, data) is True
    worker.join(10)

    assert errors == []
    assert answer == [["raced: the pass that wrote this entry finished it"]]
    assert _snapshot(bundle) == before, "control: the read's restore stands"


def test_a_completed_swap_leaves_no_journal(tmp_path: Path) -> None:
    """[if] a swap completes [then] its journal entry is gone, [else stop]."""
    data = _data_dir(tmp_path)
    _flac_bundle(data, "clean", _source(tmp_path, "a"))

    assert rc.main(["--data-dir", str(data), "--live", "--backup-dir", str(tmp_path / "backup")]) == 0

    assert rc.orphaned_swaps(data) == []


def test_a_landed_swap_whose_journal_will_not_clear_still_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the journal cannot be removed after both renames landed [then] the pass succeeds and names the entry, [else stop].

    Readers already serve the new bundle by then, so reporting the bundle as
    failed would misstate what it changed (Codex on #6820). The entry left
    behind reads as landed, and the next --live pass clears it. MUTATION
    TARGET: let the removal's error escape the landed swap.
    """
    data = _data_dir(tmp_path)
    bundle = _flac_bundle(data, "clean", _source(tmp_path, "a"))
    real_remove = rc._remove_journal

    def refuse(journal: Path) -> None:
        raise PermissionError(13, "journal directory is read-only", str(journal))

    monkeypatch.setattr(rc, "_remove_journal", refuse)
    assert rc.main(["--data-dir", str(data), "--live", "--backup-dir", str(tmp_path / "backup")]) == 0

    assert json.loads((bundle / "manifest.json").read_text())["codec"] == "mp3", "the new bundle is live"
    assert "journal entry" in capsys.readouterr().err
    assert [p.stem for p in rc.orphaned_swaps(data)] == ["clean"], "the entry stayed"
    monkeypatch.setattr(rc, "_remove_journal", real_remove)
    assert rc.recover_orphaned_swaps(data) == ["clean: swap had landed; nothing to restore"]
    assert rc.orphaned_swaps(data) == [], "control: the next pass clears it"


def test_an_interrupted_swap_is_not_repaired_while_a_stems_job_is_live(tmp_path: Path) -> None:
    """[if] a swap was interrupted and a stems job is now queued [then] --live refuses and moves nothing, recovery included, [else stop].

    MUTATION TARGET: recover before the live-job guard, and the pass writes
    into the stem root while a job may be publishing (Codex on #6820).
    """
    data = _data_dir(tmp_path, jobs=[("stems.separate", "queued")])
    _flac_bundle(data, "killed", _source(tmp_path, "a"))
    bundle, backup, before = _kill_between_the_two_renames(data, "killed", tmp_path / "old-backup")

    assert rc.main(["--data-dir", str(data), "--live", "--backup-dir", str(tmp_path / "backup")]) == 3

    assert not bundle.exists()
    assert _snapshot(backup) == before
    assert len(rc.orphaned_swaps(data)) == 1


@pytest.mark.parametrize("publisher", ["bundle_publish", "modal_vocal_farm"])
def test_a_flac_publish_over_a_recompressed_bundle_leaves_no_mp3_behind(tmp_path: Path, publisher: str) -> None:
    """[if] a FLAC publish lands on a bundle the STEM-58 pass made MP3 [then] only the new FLAC parts remain, [else stop].

    MUTATION TARGET: drop the removal of parts the new manifest does not
    name, and the old MP3 parts stay beside the new FLAC ones, holding the storage the pass
    reclaimed and making the bundle differ from its R2 index (Codex on #6820).
    """
    from apps.stems import bundle_publish

    data = _data_dir(tmp_path)
    _flac_bundle(data, "relayered", _source(tmp_path, "a"))
    assert rc.main(["--data-dir", str(data), "--live", "--backup-dir", str(tmp_path / "backup")]) == 0
    bundle = data / "state" / "stems" / "relayered"
    assert sorted(p.suffix for p in bundle.iterdir() if p.name != "manifest.json") == [".mp3"] * len(PARTS)

    publish = _the_two_flac_publishers()[publisher]
    publish(
        "relayered",
        Path("/music/a.mp3"),
        _publish_result(_fresh_parts(tmp_path, 7)),
        bundle_publish.PRESETS["hdemucs_mmi-ov0.25"],
        data,
    )

    assert sorted(p.name for p in bundle.iterdir()) == sorted([*(f"{p}.flac" for p in PARTS), "manifest.json"])
    assert load_stem_bundle("relayered", roots=[data / "state" / "stems"]).media_type == "audio/flac"


def test_a_dry_run_plan_never_mints_a_machine_id(tmp_path: Path) -> None:
    """[if] state.db has track_locations but the data dir has no machine-id file [then] planning writes nothing, [else stop].

    The location resolver's default identity lookup mints and fsyncs the id
    file, so a dry run mutated the data dir and failed on a read-only
    snapshot of one (Codex on #6820). With no id no location row can be this
    machine's, so the plan uses ``tracks.file_path``. Control: with the id
    file in place the machine-local location still wins.

    MUTATION TARGET: let ``library_paths`` resolve without passing the read id.
    """
    from apps.shared.state import db as state_db
    from apps.shared.state import machine_identity, sync_stamp
    from apps.shared.state.writer import StateWriter

    data = _data_dir(tmp_path)
    local = _source(tmp_path, "local-copy")
    recorded = _source(tmp_path, "column-copy")
    _flac_bundle(data, "snapshot-track", recorded)
    conn = state_db.open_rw(data / "state" / "state.db")
    writer = StateWriter(conn, actor="unit-test")
    try:
        writer.upsert_track(
            stable_id="snapshot-track", stable_id_tier="inferred", title="Demo",
            artists=["X"], album=None, isrc=None, duration_ms=180_000, file_path=str(local),
        )
        conn.execute("UPDATE tracks SET file_path = ? WHERE stable_id = ?", (str(recorded), "snapshot-track"))
        conn.commit()
    finally:
        writer.close()
        conn.close()

    id_file = machine_identity.machine_id_path(data)
    assert id_file.is_file(), "control: the writer minted this machine's id"
    (candidate,) = rc.plan(data)
    assert candidate.source_path == local

    id_file.unlink()
    sync_stamp.reset_machine_id_cache()
    (candidate,) = rc.plan(data)
    assert not id_file.exists(), "planning minted a machine id"
    assert (candidate.verdict, candidate.source_path) == (rc.ELIGIBLE, recorded)


def test_a_live_row_with_no_source_never_falls_back_to_the_manifest(tmp_path: Path) -> None:
    """[if] a live track row names no file and no local location materializes [then] the bundle has no source, [else stop].

    Dropping such a row made it look like a track with no library row, so
    the plan used the manifest's recorded path, which may be another
    machine's or a recording the track was relinked away from (Codex on
    #6820). Control: with the row deleted the manifest's path is still the
    fallback.

    MUTATION TARGET: drop rows with an empty path in ``library_paths``.
    """
    from apps.shared.state import db as state_db
    from apps.shared.state.writer import StateWriter

    data = _data_dir(tmp_path)
    recorded = _source(tmp_path, "recorded")
    _flac_bundle(data, "pathless-track", recorded)
    conn = state_db.open_rw(data / "state" / "state.db")
    writer = StateWriter(conn, actor="unit-test")
    try:
        writer.upsert_track(
            stable_id="pathless-track", stable_id_tier="inferred", title="Demo",
            artists=["X"], album=None, isrc=None, duration_ms=180_000, file_path=str(recorded),
        )
        conn.execute("DELETE FROM track_locations WHERE stable_id = ?", ("pathless-track",))
        conn.execute("UPDATE tracks SET file_path = NULL WHERE stable_id = ?", ("pathless-track",))
        conn.commit()
    finally:
        writer.close()
        conn.close()

    (candidate,) = rc.plan(data)
    assert (candidate.verdict, candidate.source_path) == (rc.SKIP_NO_SOURCE, None)

    conn = state_db.open_rw(data / "state" / "state.db")
    try:
        conn.execute("UPDATE tracks SET deleted_at = '2026-10-17T00:00:00+00:00' WHERE stable_id = ?", ("pathless-track",))
        conn.commit()
    finally:
        conn.close()
    (candidate,) = rc.plan(data)
    assert (candidate.verdict, candidate.source_path) == (rc.ELIGIBLE, recorded)


def test_a_source_path_failing_for_any_other_reason_is_raised_by_name(tmp_path: Path) -> None:
    """[if] stat-ing a recorded source fails for a reason other than its absence [then] the plan raises naming the path, [else stop].

    Every ``OSError`` read as "no source" turned an I/O error or a stale
    mount into an ordinary skip, and the pass exited 0 (Sol on #6820). A
    symlink loop is a real such failure any user can make: ``stat`` raises
    ``ELOOP``. Control: the too-long name, which stands for a path this
    machine cannot reach, still reads as no source.

    MUTATION TARGET: treat every ``OSError`` in ``_is_reachable_file`` as
    absence (the pre-fix source).
    """
    data = _data_dir(tmp_path)
    loop = tmp_path / "loop.mp3"
    loop.symlink_to(tmp_path / "loop-back.mp3")
    (tmp_path / "loop-back.mp3").symlink_to(loop)
    _flac_bundle(data, "looping-source", loop)
    with pytest.raises(rc.SourceUnreadableError, match="loop.mp3"):
        rc.plan(data)

    shutil.rmtree(data / "state" / "stems" / "looping-source")
    _flac_bundle(data, "unreachable-source", Path("/" + "x" * 300) / "track.mp3")
    (candidate,) = rc.plan(data)
    assert candidate.source_path is None, "control: a path this machine cannot reach is still no source"


def test_the_swap_hashes_outside_the_bundle_lock_and_refuses_a_bundle_swapped_mid_hash(tmp_path: Path) -> None:
    """[if] a writer swaps the bundle while the pass hashes before its swap [then] the writer is not held up and the swap is refused, [else stop].

    The source and bundle hashes read a whole mix and its stems, and ran
    under the striped bundle lock, so a hydrate or publish of any id on
    that stripe waited them out (Codex on #6821). The source hash here is a
    real writer on its own thread, which takes the bundle lock and swaps in
    a byte-identical copy (new inodes, same names, sizes, mtimes and
    content), so the content fingerprint still matches and only the marks
    tell the bundles apart. Control: the same swap with nobody writing lands.

    MUTATION TARGETS: hash under the lock (the writer cannot take it and
    is held up); drop the marks re-check (the copy is replaced).
    """
    import os
    import shutil
    import threading

    from apps.stems.bundle_lock import bundle_lock

    def _setup(name: str) -> tuple[Path, Path, Path]:
        bundle = tmp_path / name / "data" / "state" / "stems" / "sid"
        bundle.mkdir(parents=True)
        (bundle / "vocals.flac").write_bytes(b"old")
        os.utime(bundle / "vocals.flac", ns=(1_000_000_000, 1_000_000_000))
        new_dir = tmp_path / name / "work" / "sid"
        new_dir.mkdir(parents=True)
        (new_dir / "vocals.mp3").write_bytes(b"new")
        return bundle, new_dir, tmp_path / name / "backup" / "sid"

    bundle, new_dir, backup = _setup("untouched")
    rc._swap_in(new_dir, bundle, backup, rc.bundle_fingerprint(bundle), None, lambda: None, source_bytes_unchanged=lambda: None)
    assert (bundle / "vocals.mp3").read_bytes() == b"new", "control: an untouched bundle is swapped"

    bundle, new_dir, backup = _setup("raced")
    fingerprint = rc.bundle_fingerprint(bundle)
    copy = tmp_path / "raced" / "copy"
    shutil.copytree(bundle, copy)
    errors: list[BaseException] = []

    def swap() -> None:
        try:
            with bundle_lock(bundle):
                bundle.rename(tmp_path / "raced" / "aside")
                copy.rename(bundle)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    def hashing() -> None:
        writer = threading.Thread(target=swap)
        writer.start()
        writer.join(timeout=10)
        assert not writer.is_alive(), "a writer of this id waited out the hash"

    with pytest.raises(rc.BundleChangedError, match="while this pass verified it"):
        rc._swap_in(new_dir, bundle, backup, fingerprint, None, lambda: None, source_bytes_unchanged=hashing)
    assert not errors, errors
    assert (bundle / "vocals.flac").read_bytes() == b"old"
    assert not (bundle / "vocals.mp3").exists() and not backup.exists()


def test_the_source_is_hashed_after_the_bundle_so_a_rewrite_during_the_bundle_hash_is_caught(tmp_path: Path) -> None:
    """[if] the source check runs [then] the bundle hash has already finished, [else stop].

    On Windows a file's change time is its creation time, so a same-size
    source rewrite that puts the mtime back is seen only by the source hash;
    run before the long bundle hash, it would miss a rewrite landing during
    that hash (Codex P1 on #6820). The source check here rewrites a bundle
    part in place (same size, mtime put back): already hashed, the content
    fingerprint still matches and only the locked marks re-check refuses it.

    MUTATION TARGET: hash the source before the bundle (the fingerprint
    then sees the rewrite and refuses it as rewritten during the encode).
    """
    import os

    bundle = tmp_path / "data" / "state" / "stems" / "sid"
    bundle.mkdir(parents=True)
    part = bundle / "vocals.flac"
    part.write_bytes(b"old")
    os.utime(part, ns=(1_000_000_000, 1_000_000_000))
    new_dir = tmp_path / "work" / "sid"
    new_dir.mkdir(parents=True)
    (new_dir / "vocals.mp3").write_bytes(b"new")
    fingerprint = rc.bundle_fingerprint(bundle)

    def source_check() -> None:
        # The kernel stamps change times from a coarse clock, so a rewrite in
        # the same tick as the marks would leave the change time equal and
        # pass the re-check for a reason this test is not about (it failed so
        # on CI). Rewrite until the change time has moved; the bytes are the
        # same each time.
        recorded = part.stat().st_ctime_ns
        deadline = time.monotonic() + 5
        while part.stat().st_ctime_ns == recorded:
            assert time.monotonic() < deadline, "the change time never moved"
            time.sleep(0.001)
            with part.open("r+b") as handle:
                handle.write(b"odd")
            os.utime(part, ns=(1_000_000_000, 1_000_000_000))

    with pytest.raises(rc.BundleChangedError, match="while this pass verified it"):
        rc._swap_in(new_dir, bundle, tmp_path / "backup" / "sid", fingerprint, None, lambda: None, source_bytes_unchanged=source_check)
    assert part.read_bytes() == b"odd" and not (bundle / "vocals.mp3").exists()


# ----- STEMS-12744: live swap records the stem download index (#11411) -----


@pytest.mark.requirement("STEMS-12744")
def test_live_recompress_swap_updates_the_download_index(tmp_path: Path) -> None:
    """[if] recompress_bundle swaps an MP3 bundle [then] the index matches on-disk names and manifest sha256, [else stop]."""
    data = _data_dir(tmp_path)
    stable_id = "mp3src"
    bundle = _flac_bundle(data, stable_id, _source(tmp_path, "a"))
    save_cached_index(data, {stable_id: _stale_flac_index_entry(stable_id)})
    before_index = load_cached_index(data)

    rc.recompress_bundle((rc.plan(data))[0], tmp_path / "backup")

    cached = load_cached_index(data)
    on_disk = entry_from_bundle_dir(bundle)
    assert cached[stable_id] == on_disk
    assert ".flac" not in "".join(cached[stable_id])
    assert cached[stable_id][MANIFEST_FILENAME] != before_index[stable_id][MANIFEST_FILENAME]


@pytest.mark.requirement("STEMS-12744")
def test_stale_flac_names_in_the_cached_index_are_replaced_after_swap(tmp_path: Path) -> None:
    """[if] the cached index lists .flac names [then] a live swap rewrites them to match disk, [else stop]."""
    data = _data_dir(tmp_path)
    stable_id = "flac-names"
    _flac_bundle(data, stable_id, _source(tmp_path, "b"))
    save_cached_index(data, {stable_id: _stale_flac_index_entry(stable_id)})

    rc.recompress_bundle((rc.plan(data))[0], tmp_path / "backup")

    cached = load_cached_index(data)
    assert all(name.endswith(".mp3") for name in cached[stable_id] if name != MANIFEST_FILENAME)


@pytest.mark.requirement("STEMS-12744")
def test_a_failed_swap_leaves_the_stem_download_index_unchanged(tmp_path: Path) -> None:
    """[if] before_swap raises [then] stem-bundle-index.json is not rewritten, [else stop]."""
    data = _data_dir(tmp_path)
    stable_id = "no-index-touch"
    _flac_bundle(data, stable_id, _source(tmp_path, "c"))
    stale = _stale_flac_index_entry(stable_id)
    save_cached_index(data, {stable_id: stale})
    (candidate,) = rc.plan(data)

    def refuse() -> None:
        raise rc.StemsJobLiveError("blocked")

    with pytest.raises(rc.StemsJobLiveError):
        rc.recompress_bundle(candidate, tmp_path / "backup", before_swap=refuse)

    assert load_cached_index(data)[stable_id] == stale


@pytest.mark.requirement("STEMS-12744")
def test_bundle_changed_before_swap_leaves_the_download_index_unchanged(tmp_path: Path) -> None:
    """[if] BundleChangedError stops the swap [then] the cached index stays on the pre-swap digests, [else stop]."""
    import threading

    from apps.stems import bundle_publish

    data = _data_dir(tmp_path)
    stable_id = "racing-index"
    bundle = _long_flac_bundle(data, stable_id, tmp_path)
    stale = _stale_flac_index_entry(stable_id)
    save_cached_index(data, {stable_id: stale})
    (candidate,) = rc.plan(data)
    fresh = _publish_result(_fresh_parts(tmp_path, 8))

    def publish_mid_encode() -> None:
        _wait_for(
            lambda: any(rc.work_parent(data / "state" / "stems").glob(f".recompress-{stable_id}-*/{stable_id}")),
            what="the re-compress work directory",
        )
        bundle_publish.publish_stems_local(
            stable_id, Path("/music/a.mp3"), fresh, bundle_publish.PRESETS["hdemucs_mmi-ov0.25"], data, requested_codec="flac"
        )

    publisher = threading.Thread(target=publish_mid_encode)
    publisher.start()
    try:
        with pytest.raises(rc.BundleChangedError):
            rc.recompress_bundle(candidate, tmp_path / "backup")
    finally:
        publisher.join(timeout=60)

    assert load_cached_index(data)[stable_id] == stale


def test_the_bundle_hash_reads_its_parts_through_the_shared_read_seam(tmp_path: Path) -> None:
    """[if] the bundle hash opens a part [then] it opens it through ``open_read_shared``, [else stop].

    The hash runs outside the bundle lock, and on Windows a plain open of a
    part would make a publisher's rename of the bundle fail (Codex P1 on
    #6820). The seam refuses a symlinked part, which is what makes its use
    observable here. Control: the same bundle with a regular part hashes.

    MUTATION TARGET: go back to ``path.open("rb")``.
    """
    bundle = tmp_path / "sid"
    bundle.mkdir()
    (bundle / "vocals.flac").write_bytes(b"part")
    assert len(rc.bundle_fingerprint(bundle)) == 64, "control: a regular part hashes"
    target = tmp_path / "elsewhere.flac"
    target.write_bytes(b"part")
    (bundle / "drums.flac").symlink_to(target)
    with pytest.raises(OSError):
        rc.bundle_fingerprint(bundle)
