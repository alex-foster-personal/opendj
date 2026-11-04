"""Cloud offload (OFFLOAD, spec e8da14b8): offload, verify, restore, sweep.

Real code end to end: a real migrated state.db, real files on disk, the real
ledger, the real stem cache claim and the real stem index publisher. The only
doubles are the two object stores (:class:`InMemoryAssetS3`, whose ETag is the
body MD5 exactly as R2's is for a single-part PUT, and
:class:`apps.cloud.lock.FakeS3Client` for the CAS index pointer); the live R2
test next door (``test_cloud_offload_live.py``) checks the fake against R2.

Hard vetoes (spec section 9), one test each at least:
* [if] the upload is rejected, the size differs, the ETag differs, or the file
  changed after hashing [then] the local file is still there;
* [if] an expired temporary restore is on a deck [then] the sweep keeps it.
* [if] a restore replaces a destination whose digest differs [then] newer audio is lost;

- [if] R2 is not shown to hold a file's bytes [then] the local file is never deleted, [else stop].
"""
from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.cloud import offload, stem_index
from apps.cloud import offload_ledger as ledger
from apps.cloud.asset_store import AssetHead, AssetStoreError, asset_object_key
from apps.cloud.config import CloudConfig, MissingEnvError
from apps.cloud.lock import FakeS3Client
from apps.cloud.offload_verify import (
    RESTORE_TEMP_INFIX,
    OffloadCancelled,
    OffloadVerifyError,
    download_verified,
)
from apps.shared.state import sync_stamp

from .conftest import InMemoryAssetS3

pytestmark = pytest.mark.requirement("OFFLOAD-8598")

SID = "c" * 40
AUDIO = b"ID3-real-bytes-for-an-offload-test" * 64
ALT = b"fLaC-alternate-encode" * 32
STEMS = {"manifest.json": b'{"parts": ["vocals", "drums"]}', "vocals.mp3": b"v" * 900, "drums.mp3": b"d" * 700}
T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        return self.at


def _seed(conn: sqlite3.Connection, tmp_path: Path) -> tuple[Path, Path, Path]:
    audio = tmp_path / "music" / "track.mp3"
    alt = tmp_path / "music" / "track.flac"
    for path, body in ((audio, AUDIO), (alt, ALT)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    machine = sync_stamp.ensure_local_machine(conn)
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
        "VALUES (?, 'inferred', 'offload me', ?, 't0', 't0')",
        (SID, str(audio)),
    )
    conn.execute(
        "INSERT INTO track_locations(stable_id, machine_id, kind, role, file_path, available, "
        "created_at, updated_at) VALUES (?, ?, 'local', 'alternate', ?, 1, 't0', 't0')",
        (SID, machine, str(alt)),
    )
    conn.commit()
    stems = tmp_path / "stems" / SID
    stems.mkdir(parents=True)
    for name, body in STEMS.items():
        (stems / name).write_bytes(body)
    return audio, alt, stems


def _ctx(tmp_path: Path, cfg: CloudConfig, s3: InMemoryAssetS3, index: FakeS3Client, **over) -> offload.OffloadContext:
    base = dict(
        data_dir=tmp_path,
        state_db=tmp_path / "state.db",
        stems_dir=tmp_path / "stems",
        cfg=cfg,
        assets=s3,
        index_s3=index,
        protected_ids=lambda: frozenset(),
        gate_reason=lambda: None,
        now=_Clock(),
    )
    base.update(over)
    return offload.OffloadContext(**base)


def _state(tmp_path: Path) -> ledger.TrackOffload | None:
    with ledger.open_ledger(tmp_path) as conn:
        return ledger.get_track(conn, SID)


def _available(conn: sqlite3.Connection, path: Path) -> int:
    return conn.execute(
        "SELECT available FROM track_locations WHERE stable_id = ? AND kind = 'local' AND file_path = ?",
        (SID, str(path)),
    ).fetchone()[0]


# --- offload ------------------------------------------------------------------


def test_offload_uploads_verifies_then_deletes_every_local_file(conn, cfg, fake_s3, tmp_path):
    """[if] a track with audio, an alternate and stems is offloaded [then] R2 holds
    all of them, the local copies are gone, the state is offloaded, the remote row
    and the stem index name them [else stop]."""
    audio, alt, stems = _seed(conn, tmp_path)
    index = FakeS3Client()
    outcome = offload.offload_track(_ctx(tmp_path, cfg, fake_s3, index), SID)

    assert outcome.state == "offloaded"
    assert outcome.files == 2 + len(STEMS)
    assert outcome.bytes_freed == len(AUDIO) + len(ALT) + sum(map(len, STEMS.values()))
    assert not audio.exists() and not alt.exists() and not stems.exists()
    for body in (AUDIO, ALT, *STEMS.values()):
        key = asset_object_key(hashlib.sha256(body).hexdigest())
        assert fake_s3.store[(cfg.audio_bucket, key)][0] == body
    assert _state(tmp_path).state == "offloaded"
    assert _available(conn, alt) == 0
    remote = conn.execute(
        "SELECT COUNT(*) FROM track_locations WHERE stable_id = ? AND kind = 'remote'", (SID,)
    ).fetchone()[0]
    assert remote == 2
    published = stem_index.fetch_index(cfg, index)[SID]
    assert published == {n: hashlib.sha256(b).hexdigest() for n, b in STEMS.items()}
    assert stem_index.load_cached_index(tmp_path)[SID] == published


def test_offload_twice_is_a_no_op(conn, cfg, fake_s3, tmp_path):
    _seed(conn, tmp_path)
    ctx = _ctx(tmp_path, cfg, fake_s3, FakeS3Client())
    offload.offload_track(ctx, SID)
    puts = len(fake_s3.put_calls)
    again = offload.offload_track(ctx, SID)
    assert again.note == "already offloaded"
    assert len(fake_s3.put_calls) == puts


def test_rejected_upload_keeps_every_local_file(conn, cfg, tmp_path):
    """VETO: [if] R2 refuses the upload [then] nothing local is deleted."""
    audio, alt, stems = _seed(conn, tmp_path)
    s3 = InMemoryAssetS3(fail_puts=True)
    with pytest.raises(AssetStoreError, match="rejected"):
        offload.offload_track(_ctx(tmp_path, cfg, s3, FakeS3Client()), SID)
    assert audio.read_bytes() == AUDIO and alt.read_bytes() == ALT and stems.is_dir()
    row = _state(tmp_path)
    assert row.state == "local" and row.error


class _CorruptingS3(InMemoryAssetS3):
    """Stores what it is given, then answers HEAD with a damaged size or ETag."""

    def __init__(self, *, size_delta: int = 0, etag: str | None = None) -> None:
        super().__init__()
        self.size_delta, self.etag = size_delta, etag

    def head_object(self, bucket, key):
        head = super().head_object(bucket, key)
        if head is None:
            return None
        return AssetHead(size=head.size + self.size_delta, etag=self.etag or head.etag)


@pytest.mark.parametrize(
    "s3",
    [_CorruptingS3(size_delta=-1), _CorruptingS3(etag='"' + "0" * 32 + '"')],
    ids=["size-differs", "etag-differs"],
)
def test_unverifiable_copy_keeps_the_local_file(conn, cfg, tmp_path, s3):
    """VETO: [if] R2's size or content does not match [then] the file stays."""
    audio, alt, _stems = _seed(conn, tmp_path)
    with pytest.raises(OffloadVerifyError):
        offload.offload_track(_ctx(tmp_path, cfg, s3, FakeS3Client()), SID)
    assert audio.read_bytes() == AUDIO and alt.read_bytes() == ALT
    assert _state(tmp_path).state == "local"


def test_multipart_etag_is_verified_by_streaming_the_object(conn, cfg, tmp_path):
    """[if] the ETag is not a plain MD5 [then] the object is streamed and hashed."""
    s3 = _CorruptingS3(etag='"abc123-2"')
    audio, _alt, _stems = _seed(conn, tmp_path)
    outcome = offload.offload_track(_ctx(tmp_path, cfg, s3, FakeS3Client()), SID)
    assert outcome.state == "offloaded" and not audio.exists()
    with ledger.open_ledger(tmp_path) as led:
        assert {f.verified_by for f in ledger.files_for(led, SID)} == {"stream-sha256"}


def test_file_changed_after_hashing_is_kept(conn, cfg, fake_s3, tmp_path):
    """VETO: [if] the file is rewritten after it was hashed and verified (a re-tag
    just before deletion) [then] it is not deleted."""
    audio, _alt, _stems = _seed(conn, tmp_path)
    calls = {"n": 0}

    def retag_at_delete_time() -> frozenset[str]:
        calls["n"] += 1
        if calls["n"] == 2:  # the pre-delete gate check: every upload is verified by now
            audio.write_bytes(AUDIO + b"retagged")
        return frozenset()

    ctx = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), protected_ids=retag_at_delete_time)
    with pytest.raises(OffloadVerifyError, match="changed since hashing"):
        offload.offload_track(ctx, SID)
    assert audio.read_bytes() == AUDIO + b"retagged"
    assert _state(tmp_path).state == "local"


def test_track_on_a_deck_is_refused_before_any_upload(conn, cfg, fake_s3, tmp_path):
    audio, *_ = _seed(conn, tmp_path)
    ctx = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), protected_ids=lambda: frozenset({SID}))
    with pytest.raises(offload.OffloadRefused) as refused:
        offload.offload_track(ctx, SID)
    assert refused.value.code == "TRACK_ON_DECK"
    assert fake_s3.put_calls == [] and audio.exists()


def test_gig_posture_defers_an_offload(conn, cfg, fake_s3, tmp_path):
    audio, *_ = _seed(conn, tmp_path)
    ctx = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), gate_reason=lambda: "gig_posture")
    with pytest.raises(offload.OffloadRefused) as refused:
        offload.offload_track(ctx, SID)
    assert refused.value.code == "OFFLOAD_DEFERRED" and audio.exists()


def test_missing_credentials_fail_loudly_and_keep_files(conn, fake_s3, tmp_path):
    audio, *_ = _seed(conn, tmp_path)
    blank = CloudConfig("", "", "", "s", "a", "h", "127.0.0.1")
    with pytest.raises(MissingEnvError):
        offload.offload_track(_ctx(tmp_path, blank, fake_s3, FakeS3Client()), SID)
    assert audio.exists()


def test_resume_after_crash_reuses_verified_uploads(conn, cfg, fake_s3, tmp_path):
    """[if] the engine died mid-offload [then] a resume finishes without re-uploading."""
    audio, *_ = _seed(conn, tmp_path)
    ctx = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), protected_ids=lambda: frozenset())
    calls = {"n": 0}

    def deck_appears_at_delete() -> frozenset[str]:
        calls["n"] += 1
        return frozenset({SID}) if calls["n"] > 1 else frozenset()

    crashing = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), protected_ids=deck_appears_at_delete)
    with pytest.raises(offload.OffloadRefused):
        offload.offload_track(crashing, SID)
    assert audio.exists()
    puts = len(fake_s3.put_calls)
    assert offload.offload_track(ctx, SID).state == "offloaded"
    assert len(fake_s3.put_calls) == puts  # every object was already verified in R2


# --- restore -------------------------------------------------------------------


def _offloaded(conn, cfg, fake_s3, tmp_path, **over):
    paths = _seed(conn, tmp_path)
    ctx = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), **over)
    offload.offload_track(ctx, SID)
    return ctx, paths


def test_download_for_24h_restores_byte_identical_files(conn, cfg, fake_s3, tmp_path):
    ctx, (audio, alt, stems) = _offloaded(conn, cfg, fake_s3, tmp_path)
    outcome = offload.restore_track(ctx, SID, "temporary")
    assert outcome.state == "temporary"
    assert audio.read_bytes() == AUDIO and alt.read_bytes() == ALT
    assert {p.name: p.read_bytes() for p in stems.iterdir()} == STEMS
    row = _state(tmp_path)
    assert row.state == "temporary"
    assert ledger.parse_iso(row.expires_at) == T0 + timedelta(hours=24)
    assert _available(conn, alt) == 1


def test_redownload_permanently_clears_the_offloaded_state(conn, cfg, fake_s3, tmp_path):
    ctx, (audio, *_rest) = _offloaded(conn, cfg, fake_s3, tmp_path)
    assert offload.restore_track(ctx, SID, "permanent").state == "local"
    assert audio.read_bytes() == AUDIO
    with ledger.open_ledger(tmp_path) as led:
        assert ledger.get_track(led, SID).state == "local"
        assert ledger.files_for(led, SID) == []


def test_cancelled_restore_leaves_no_partial_file(conn, cfg, fake_s3, tmp_path):
    ctx, (audio, alt, _stems) = _offloaded(conn, cfg, fake_s3, tmp_path)
    cancelling = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), cancelled=lambda: True)
    with pytest.raises(OffloadCancelled):
        offload.restore_track(cancelling, SID, "temporary")
    assert not audio.exists() and not alt.exists()
    assert list(audio.parent.iterdir()) == []  # no temp file left behind
    assert _state(tmp_path).state == "offloaded"
    assert offload.restore_track(ctx, SID, "temporary").state == "temporary"


def test_corrupt_download_is_never_renamed_into_place(conn, cfg, fake_s3, tmp_path):
    ctx, (audio, *_rest) = _offloaded(conn, cfg, fake_s3, tmp_path)
    key = asset_object_key(hashlib.sha256(AUDIO).hexdigest())
    fake_s3.store[(cfg.audio_bucket, key)] = (b"bit-rot", '"x"')
    with pytest.raises(OffloadVerifyError):
        offload.restore_track(ctx, SID, "temporary")
    assert not audio.exists()


# --- sweep ---------------------------------------------------------------------


def test_sweep_reoffloads_an_expired_temporary_restore_without_reuploading(conn, cfg, fake_s3, tmp_path):
    ctx, (audio, *_rest) = _offloaded(conn, cfg, fake_s3, tmp_path)
    offload.restore_track(ctx, SID, "temporary")
    puts = len(fake_s3.put_calls)
    ctx.now.at = T0 + timedelta(hours=25)
    report = offload.sweep(ctx)
    assert report.reoffloaded == [SID]
    assert not audio.exists() and _state(tmp_path).state == "offloaded"
    assert len(fake_s3.put_calls) == puts


def test_sweep_never_reoffloads_a_track_on_a_deck(conn, cfg, fake_s3, tmp_path):
    """VETO: [if] an expired temporary track is on a deck [then] it stays and its
    expiry moves to 24 h after now (the last play)."""
    ctx, (audio, *_rest) = _offloaded(conn, cfg, fake_s3, tmp_path)
    offload.restore_track(ctx, SID, "temporary")
    on_deck = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), protected_ids=lambda: frozenset({SID}), now=ctx.now)
    on_deck.now.at = T0 + timedelta(hours=30)
    report = offload.sweep(on_deck)
    assert report.extended == [SID] and report.reoffloaded == []
    assert audio.read_bytes() == AUDIO
    assert ledger.parse_iso(_state(tmp_path).expires_at) == T0 + timedelta(hours=54)


def test_sweep_during_a_gig_deletes_nothing(conn, cfg, fake_s3, tmp_path):
    ctx, (audio, *_rest) = _offloaded(conn, cfg, fake_s3, tmp_path)
    offload.restore_track(ctx, SID, "temporary")
    gig = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), gate_reason=lambda: "gig_posture", now=ctx.now)
    gig.now.at = T0 + timedelta(hours=30)
    report = offload.sweep(gig)
    assert report.deferred_reason == "gig_posture" and report.reoffloaded == []
    assert audio.exists()


def test_offload_states_reads_nothing_when_no_ledger_exists(tmp_path):
    assert offload.offload_states(tmp_path, [SID]) == {}
    assert not ledger.ledger_path(tmp_path).exists()


# --- review fixes (#8598 review round 1) ---------------------------------------


def test_stems_rewritten_after_upload_are_kept_and_audio_too(conn, cfg, fake_s3, tmp_path):
    """Codex P1: [if] a republish adds a part to the bundle after the others were
    verified (every verified file untouched, so only the guarded claim's fingerprint
    can see it) [then] the bundle is kept AND no audio is deleted first, [else stop]."""
    audio, alt, stems = _seed(conn, tmp_path)
    calls = {"n": 0}

    def republish_at_delete_time() -> frozenset[str]:
        calls["n"] += 1
        if calls["n"] == 2:  # the pre-delete gate: every part is verified by now
            (stems / "bass.mp3").write_bytes(b"new part, never uploaded" * 40)
        return frozenset()

    ctx = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), protected_ids=republish_at_delete_time)
    with pytest.raises(offload.OffloadRefused) as refused:
        offload.offload_track(ctx, SID)
    assert refused.value.code == "STEMS_CHANGED"
    assert (stems / "bass.mp3").exists() and (stems / "vocals.mp3").read_bytes() == STEMS["vocals.mp3"]
    assert audio.read_bytes() == AUDIO and alt.read_bytes() == ALT
    assert _state(tmp_path).state == "local"


def test_resume_reconciles_a_deletion_the_ledger_never_recorded(conn, cfg, fake_s3, tmp_path):
    """Codex P1: [if] the engine died between unlink() and its ledger write [then]
    resume records the verified, missing file as deleted and restore brings it back, [else stop]."""
    audio, _alt, _stems = _seed(conn, tmp_path)
    ctx = _ctx(tmp_path, cfg, fake_s3, FakeS3Client())
    offload.offload_track(ctx, SID)
    with ledger.open_ledger(tmp_path) as led:  # the crash: deletions never recorded
        led.execute("UPDATE offload_files SET deleted_at = NULL WHERE stable_id = ?", (SID,))
        ledger.set_state(led, SID, "uploading", now=T0)
    assert offload.offload_track(ctx, SID).state == "offloaded"
    with ledger.open_ledger(tmp_path) as led:
        assert all(f.deleted_at for f in ledger.files_for(led, SID))
    assert offload.restore_track(ctx, SID, "temporary").state == "temporary"
    assert audio.read_bytes() == AUDIO


def test_cancel_after_the_last_upload_deletes_nothing(conn, cfg, fake_s3, tmp_path):
    """Codex P2: [if] cancel lands after every upload verified [then] nothing is deleted, [else stop]."""
    audio, alt, stems = _seed(conn, tmp_path)
    every_file = 2 + len(STEMS)
    # Cancel lands once the last upload has started: every file uploads and verifies.
    ctx = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), cancelled=lambda: len(fake_s3.put_calls) >= every_file)
    with pytest.raises(OffloadCancelled, match="before deleting"):
        offload.offload_track(ctx, SID)
    assert len(fake_s3.put_calls) == every_file
    assert audio.exists() and alt.exists() and stems.is_dir()


def test_one_failing_track_never_stops_the_sweep(conn, cfg, fake_s3, tmp_path):
    """Sentry: [if] one track raises an unexpected error [then] the sweep records it and returns, [else stop]."""
    ctx, (audio, *_rest) = _offloaded(conn, cfg, fake_s3, tmp_path)
    offload.restore_track(ctx, SID, "temporary")
    calls = {"n": 0}

    def registry_breaks_inside_the_offload() -> frozenset[str]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("deck registry unreadable")
        return frozenset()

    broken = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), protected_ids=registry_breaks_inside_the_offload, now=ctx.now)
    broken.now.at = T0 + timedelta(hours=25)
    report = offload.sweep(broken)
    assert report.reoffloaded == [] and "RuntimeError" in report.skipped[SID]
    assert audio.exists()


# --- review fixes (#8598 review round 2) ---------------------------------------


def test_audio_rewritten_during_the_stem_claim_is_kept(conn, cfg, fake_s3, tmp_path):
    """Codex P1: [if] the audio is rewritten while the stem claim runs (after the
    pre-delete check) [then] the rewritten file is kept, [else stop]."""
    audio, _alt, _stems = _seed(conn, tmp_path)
    calls = {"n": 0}

    def retag_inside_the_stem_claim() -> frozenset[str]:
        calls["n"] += 1
        if calls["n"] == 3:  # claim_if_unchanged's live deck check, after unchanged_since_hash
            audio.write_bytes(AUDIO + b"retagged-in-place")
        return frozenset()

    ctx = _ctx(tmp_path, cfg, fake_s3, FakeS3Client(), protected_ids=retag_inside_the_stem_claim)
    with pytest.raises(OffloadVerifyError, match="changed after it was verified"):
        offload.offload_track(ctx, SID)
    assert audio.read_bytes() == AUDIO + b"retagged-in-place"
    assert not list(audio.parent.glob("*.offload-deleting-*"))


def test_a_partly_failed_restore_still_marks_the_restored_copy_available(conn, cfg, fake_s3, tmp_path):
    """Codex P2: [if] the second audio download fails [then] the first, already back,
    reads available, [else stop]."""
    ctx, (audio, alt, _stems) = _offloaded(conn, cfg, fake_s3, tmp_path)
    key = asset_object_key(hashlib.sha256(AUDIO).hexdigest())
    fake_s3.store[(cfg.audio_bucket, key)] = (b"bit-rot", '"x"')
    with pytest.raises(OffloadVerifyError):
        offload.restore_track(ctx, SID, "temporary")
    assert alt.read_bytes() == ALT and not audio.exists()
    assert _available(conn, alt) == 1


def test_resume_marks_every_deleted_audio_copy_unavailable(conn, cfg, fake_s3, tmp_path):
    """Codex P2: [if] a crash left one deleted copy marked available [then] finishing
    the resumed offload marks it unavailable too, [else stop]."""
    audio, alt, _stems = _seed(conn, tmp_path)
    ctx = _ctx(tmp_path, cfg, fake_s3, FakeS3Client())
    offload.offload_track(ctx, SID)
    # The crash: the alternate was deleted but its row never flipped, and the
    # main copy was never deleted.
    conn.execute("UPDATE track_locations SET available = 1 WHERE stable_id = ? AND file_path = ?", (SID, str(alt)))
    conn.commit()
    audio.write_bytes(AUDIO)
    with ledger.open_ledger(tmp_path) as led:
        led.execute("UPDATE offload_files SET deleted_at = NULL WHERE stable_id = ? AND local_path = ?", (SID, str(audio)))
        ledger.set_state(led, SID, "uploading", now=T0)
    assert offload.offload_track(ctx, SID).state == "offloaded"
    assert _available(conn, alt) == 0 and not audio.exists()


# --- review fixes (#8598 review round 3) ---------------------------------------


def test_restore_keeps_a_newer_file_at_the_destination(conn, cfg, fake_s3, tmp_path):
    """[if] a newer file occupies the restore dest [then] restore keeps it, [else stop]."""
    ctx, (audio, alt, stems) = _offloaded(conn, cfg, fake_s3, tmp_path)
    newer = AUDIO + b"retag"
    audio.write_bytes(newer)
    outcome = offload.restore_track(ctx, SID, "temporary")
    assert audio.read_bytes() == newer
    assert alt.read_bytes() == ALT
    assert {p.name: p.read_bytes() for p in stems.iterdir()} == STEMS
    assert outcome.state == "temporary" and outcome.note == "kept newer local file"
    assert _state(tmp_path).state == "temporary"
    with ledger.open_ledger(tmp_path) as led:
        row = led.execute(
            "SELECT deleted_at FROM offload_files WHERE stable_id = ? AND local_path = ?",
            (SID, str(audio)),
        ).fetchone()
        assert row is not None and row[0] is None
    assert _available(conn, alt) == 1


def test_resume_accepts_an_already_restored_file_without_redownloading(conn, cfg, fake_s3, tmp_path):
    """[if] dest already holds the recorded bytes [then] restore skips the GET, [else stop]."""
    ctx, (audio, _alt, _stems) = _offloaded(conn, cfg, fake_s3, tmp_path)
    audio.write_bytes(AUDIO)
    with ledger.open_ledger(tmp_path) as led:
        ledger.set_state(led, SID, "restoring", now=T0, restore_mode="temporary")

    class _CountingS3(InMemoryAssetS3):
        def __init__(self, inner: InMemoryAssetS3) -> None:
            self.store = inner.store
            self.audio_gets = 0

        def download_to(self, bucket: str, key: str, handle, on_chunk) -> None:
            if key == asset_object_key(hashlib.sha256(AUDIO).hexdigest()):
                self.audio_gets += 1
            return super().download_to(bucket, key, handle, on_chunk)

    counting = _CountingS3(fake_s3)
    resume_ctx = _ctx(tmp_path, cfg, counting, FakeS3Client(), now=ctx.now)
    outcome = offload.restore_track(resume_ctx, SID, "temporary")
    assert audio.read_bytes() == AUDIO
    assert outcome.state == "temporary" and outcome.note is None
    assert counting.audio_gets == 0


def test_resume_keeps_an_edit_made_after_a_partial_restore(conn, cfg, fake_s3, tmp_path):
    """[if] dest was edited after a crash between publish and mark_restored [then] edit is kept, [else stop]."""
    ctx, (audio, alt, _stems) = _offloaded(conn, cfg, fake_s3, tmp_path)
    edited = AUDIO + b"edited-after-publish"
    audio.write_bytes(edited)
    with ledger.open_ledger(tmp_path) as led:
        ledger.set_state(led, SID, "restoring", now=T0, restore_mode="temporary")
    outcome = offload.restore_track(ctx, SID, "temporary")
    assert audio.read_bytes() == edited
    assert outcome.note == "kept newer local file" and outcome.state == "temporary"
    assert alt.read_bytes() == ALT


def test_a_file_created_during_download_is_not_overwritten(cfg, fake_s3, tmp_path):
    """[if] dest is created while the object streams [then] that file is kept, [else stop]."""
    sha = hashlib.sha256(AUDIO).hexdigest()
    key = asset_object_key(sha)
    fake_s3.store[(cfg.audio_bucket, key)] = (AUDIO, fake_s3._etag(AUDIO))
    dest = tmp_path / "track.mp3"
    mid = b"created-mid-stream"

    class _MidStreamS3(InMemoryAssetS3):
        def __init__(self, inner: InMemoryAssetS3) -> None:
            self.store = inner.store

        def download_to(self, bucket: str, key: str, handle, on_chunk) -> None:
            got = self.store.get((bucket, key))
            if got is None:
                raise KeyError(f"{bucket}/{key} is not in the store")
            body = got[0]
            for chunk_index, start in enumerate(range(0, len(body), 256)):
                chunk = body[start : start + 256]
                handle.write(chunk)
                on_chunk(len(chunk))
                if chunk_index == 0 and not dest.exists():
                    dest.write_bytes(mid)

    result = download_verified(
        cfg,
        _MidStreamS3(fake_s3),
        sha,
        dest,
        on_chunk=lambda _n: None,
        cancelled=lambda: False,
    )
    assert dest.read_bytes() == mid
    assert result.outcome == "preserved"
    assert not list(dest.parent.glob(f"*{RESTORE_TEMP_INFIX}*"))


def test_same_digest_at_destination_is_accepted(cfg, fake_s3, tmp_path):
    """[if] dest already matches the key [then] restore accepts it without a GET, [else stop]."""
    sha = hashlib.sha256(AUDIO).hexdigest()
    dest = tmp_path / "track.mp3"
    dest.write_bytes(AUDIO)
    fake_s3.store[(cfg.audio_bucket, asset_object_key(sha))] = (AUDIO, fake_s3._etag(AUDIO))

    class _CountingS3(InMemoryAssetS3):
        download_calls = 0

        def download_to(self, bucket: str, key: str, handle, on_chunk) -> None:
            type(self).download_calls += 1
            return super().download_to(bucket, key, handle, on_chunk)

    counting = _CountingS3()
    counting.store = fake_s3.store
    result = download_verified(
        cfg, counting, sha, dest, on_chunk=lambda _n: None, cancelled=lambda: False
    )
    assert result.outcome == "already_present" and result.bytes_written == 0
    assert dest.read_bytes() == AUDIO
    assert counting.download_calls == 0


@pytest.mark.parametrize(
    ("dest_body", "expected_outcome"),
    [(b"different-bytes", "preserved"), (None, "published")],
)
def test_publish_without_hard_links_still_keeps_a_differing_dest(
    cfg, fake_s3, tmp_path, monkeypatch, dest_body, expected_outcome
):
    """[if] hard links are unavailable [then] differing dest is kept and absent dest still publishes, [else stop]."""
    sha = hashlib.sha256(AUDIO).hexdigest()
    key = asset_object_key(sha)
    fake_s3.store[(cfg.audio_bucket, key)] = (AUDIO, fake_s3._etag(AUDIO))
    dest = tmp_path / "track.mp3"
    if dest_body is not None:
        dest.write_bytes(dest_body)

    def _no_link(_src, _dst) -> None:
        raise OSError(errno.EPERM, "not supported")

    monkeypatch.setattr(os, "link", _no_link)
    result = download_verified(
        cfg, fake_s3, sha, dest, on_chunk=lambda _n: None, cancelled=lambda: False
    )
    assert result.outcome == expected_outcome
    if expected_outcome == "preserved":
        assert dest.read_bytes() == dest_body
    else:
        assert dest.read_bytes() == AUDIO
