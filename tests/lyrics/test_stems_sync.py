"""stems_sync: push/hydrate without moving local files or writing track_locations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import wave
from pathlib import Path

import pytest

from apps.cloud import asset_store
from apps.cloud.config import CloudConfig
from apps.cloud.eviction import HydrationError
from apps.lyrics import register_stems, stems_sync
from apps.shared.state import db as state_db
from apps.stems.artifacts import load_stem_bundle

from .conftest import (
    InMemoryAssetS3,
    seed_stamped_policy,
    seed_track,
    use_cloud_mode,
    use_local_mode,
)


def _test_cfg() -> CloudConfig:
    return CloudConfig(
        r2_account_id="acct1234",
        r2_access_key_id="AKIDEXAMPLE",
        r2_secret_access_key="secret-key",
        state_bucket="test-state",
        audio_bucket="test-audio",
        hostname="test-host",
        bind_host="127.0.0.1",
    )


def _write_wav(path: Path, seconds: float = 1.0) -> None:
    frames = int(seconds * 8000)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(
            b"".join(
                int(3000 * math.sin(i / 20)).to_bytes(2, "little", signed=True) * 2
                for i in range(frames)
            )
        )


# REQ: LYR-02
def test_push_missing_dry_run_lists_work_without_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    use_local_mode(monkeypatch)
    data_dir = tmp_path / "data"
    external = tmp_path / "external-stems"
    external.mkdir()
    monkeypatch.setenv("MDT_EXTERNAL_STEM_ROOTS", str(external))
    stems_root = data_dir / "state" / "stems-roformer-spike" / "sid1"
    stems_root.mkdir(parents=True)
    _write_wav(stems_root / "vocals.wav")
    _write_wav(stems_root / "instrumental.wav")
    (stems_root / "manifest.json").write_text(
        json.dumps({
            "schema_version": 3,
            "stable_id": "sid1",
            "layout": "roformer2",
            "model": {"name": "m", "version": "v"},
            "source": {"path": "/x/a.wav", "sha256": "0" * 64},
            "files": {"vocals": "vocals.wav", "instrumental": "instrumental.wav"},
            "audio": {"sample_rate": 8000, "channels": 2, "frame_count": 8000},
        }),
        encoding="utf-8",
    )
    rc = stems_sync.push_missing(data_dir=data_dir, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "local:" in out
    assert "dry run: no credentials used, nothing uploaded" in out


def _seed_s3_object(fake_s3: InMemoryAssetS3, cfg: CloudConfig, path: Path) -> str:
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    key = asset_store.asset_object_key(digest)
    fake_s3.store[(cfg.audio_bucket, key)] = (body, fake_s3._etag(body))
    return digest


def test_hydrate_fetches_by_hash_and_passes_strict_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_s3: InMemoryAssetS3,
) -> None:
    use_local_mode(monkeypatch)
    cfg = _test_cfg()
    data_dir = tmp_path / "data"
    stems_root = data_dir / "state" / "stems-roformer-spike"
    source_root = tmp_path / "source"
    source_root.mkdir()
    _write_wav(source_root / "v.wav", 1.0)
    _write_wav(source_root / "i.wav", 1.0)
    bundle_dir = register_stems.register_pair(
        stable_id="sid-hydrate",
        vocals=source_root / "v.wav",
        instrumental=source_root / "i.wav",
        model_name="m",
        model_version="v",
        source_path=str(source_root / "v.wav"),
        storage=register_stems.RegisterPairStorage(root=tmp_path / "unused-root"),
    )
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    external_manifest = tmp_path / "manifest.json"
    external_manifest.write_text(
        (bundle_dir / "manifest.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    for _part, rel in manifest["files"].items():
        _seed_s3_object(fake_s3, cfg, bundle_dir / rel)

    use_cloud_mode(monkeypatch)
    monkeypatch.setattr(
        "apps.lyrics.stems_sync.asset_clients_for_mode",
        lambda writing=True: (fake_s3, cfg),
    )
    dest_root = stems_root
    assert not (dest_root / "sid-hydrate").exists()
    rc = stems_sync.hydrate(
        "sid-hydrate",
        manifest_path=external_manifest,
        data_dir=data_dir,
        dry_run=False,
    )
    assert rc == 0
    bundle = load_stem_bundle("sid-hydrate", stems_dir=dest_root)
    assert set(bundle.files) == {"vocals", "instrumental"}
    landed = sorted(p.name for p in (dest_root / "sid-hydrate").iterdir())

    # Hydrating over a bundle that holds other files (one STEM-58 re-compressed)
    # replaces it whole: nothing of the old bundle is left outside the manifest.
    (dest_root / "sid-hydrate" / "vocals.mp3").write_bytes(b"stale part")
    # Another track's hydrate in flight in this same process (the webui serves
    # several at once, each under its own bundle lock) must not lose its files.
    in_flight = data_dir / "state" / stems_sync.HYDRATE_STAGING_DIR_NAME / str(os.getpid()) / "sid-other"
    in_flight.mkdir(parents=True)
    (in_flight / "vocals.wav").write_bytes(b"half fetched")
    rc = stems_sync.hydrate("sid-hydrate", manifest_path=external_manifest, data_dir=data_dir, dry_run=False)
    assert rc == 0
    assert sorted(p.name for p in (dest_root / "sid-hydrate").iterdir()) == landed
    assert landed == sorted([*manifest["files"].values(), "manifest.json"]), "control: the parts did land"
    assert (in_flight / "vocals.wav").read_bytes() == b"half fetched", "another hydrate's staging is untouched"
    staging_root = data_dir / "state" / stems_sync.HYDRATE_STAGING_DIR_NAME
    assert sorted(p.name for p in staging_root.iterdir()) == [str(os.getpid())], "no staging copy of ours is left behind"

    # A stem root that is a symlink to another disk stages on THAT disk, beside
    # the real root, so the publishing rename never crosses filesystems (EXDEV)
    # (Codex on #6820). MUTATION TARGET: stage in the lexical stems_root.parent.
    disk = tmp_path / "disk"
    disk.mkdir()
    real_root = disk / "stems-roformer-spike"
    stems_root.rename(real_root)
    stems_root.symlink_to(real_root, target_is_directory=True)
    shutil.rmtree(data_dir / "state" / ".stem-bundle-swap")  # what the unlinked hydrate above left
    rc = stems_sync.hydrate("sid-hydrate", manifest_path=external_manifest, data_dir=data_dir, dry_run=False)
    assert rc == 0
    assert sorted(p.name for p in (real_root / "sid-hydrate").iterdir()) == landed, "control: it hydrated through the link"
    assert (disk / stems_sync.HYDRATE_STAGING_DIR_NAME).is_dir(), "staged beside the real root"
    assert sorted(p.name for p in staging_root.iterdir()) == [str(os.getpid())], "nothing staged on the data dir's disk"
    assert (disk / ".stem-bundle-swap").is_dir(), "the old bundle was set aside on the real root's disk"
    assert not (data_dir / "state" / ".stem-bundle-swap").exists(), "never on the data dir's disk"


def test_hydrate_dry_run_writes_nothing(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({
            "schema_version": 3,
            "files": {"vocals": "vocals.wav", "instrumental": "instrumental.wav"},
            "files_sha256": {"vocals": "a" * 64, "instrumental": "b" * 64},
        }),
        encoding="utf-8",
    )
    rc = stems_sync.hydrate(
        "sid1",
        manifest_path=manifest,
        data_dir=tmp_path / "data",
        dry_run=True,
    )
    assert rc == 0
    assert not (tmp_path / "data" / "state" / "stems-roformer-spike" / "sid1").exists()


def test_register_cloud_push_verifies_without_track_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_s3: InMemoryAssetS3,
) -> None:
    use_cloud_mode(monkeypatch)
    data_dir = tmp_path / "data"
    db_path = data_dir / "state" / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        seed_track(conn, "sid-cloud")
        seed_stamped_policy(conn, asset_kind="stem_bundle", mode="pinned")
    finally:
        conn.close()

    voc = tmp_path / "v.wav"
    ins = tmp_path / "i.wav"
    _write_wav(voc)
    _write_wav(ins)
    root = data_dir / "state" / "stems-roformer-spike"

    cfg = _test_cfg()
    register_stems.register_pair(
        stable_id="sid-cloud",
        vocals=voc,
        instrumental=ins,
        model_name="m",
        model_version="v",
        source_path=str(voc),
        storage=register_stems.RegisterPairStorage(
            root=root, s3=fake_s3, cfg=cfg, conn=state_db.open_rw(db_path)
        ),
    )
    conn = state_db.open_rw(db_path)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM track_locations").fetchone()[0]
    finally:
        conn.close()
    assert rows == 0
    assert voc.is_file() and ins.is_file()
    assert len(fake_s3.put_calls) >= 2
    manifest = json.loads((root / "sid-cloud" / "manifest.json").read_text())
    for digest in manifest["files_sha256"].values():
        assert asset_store.object_exists(cfg, fake_s3, digest)
    manifest_digest = hashlib.sha256(
        (root / "sid-cloud" / "manifest.json").read_bytes()
    ).hexdigest()
    assert asset_store.object_exists(cfg, fake_s3, manifest_digest)


def test_register_cloud_excluded_pushes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_s3: InMemoryAssetS3,
) -> None:
    use_cloud_mode(monkeypatch)
    data_dir = tmp_path / "data"
    db_path = data_dir / "state" / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        seed_track(conn, "sid-excl")
        seed_stamped_policy(conn, asset_kind="stem_bundle", mode="excluded")
    finally:
        conn.close()

    voc = tmp_path / "v.wav"
    ins = tmp_path / "i.wav"
    _write_wav(voc)
    _write_wav(ins)
    root = data_dir / "state" / "stems-roformer-spike"
    register_stems.register_pair(
        stable_id="sid-excl",
        vocals=voc,
        instrumental=ins,
        model_name="m",
        model_version="v",
        source_path=str(voc),
        storage=register_stems.RegisterPairStorage(
            root=root, s3=fake_s3, cfg=_test_cfg(), conn=state_db.open_rw(db_path)
        ),
    )
    assert fake_s3.put_calls == []


def test_register_cloud_stream_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_s3: InMemoryAssetS3,
) -> None:
    use_cloud_mode(monkeypatch)
    data_dir = tmp_path / "data"
    db_path = data_dir / "state" / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        seed_track(conn, "sid-stream")
        seed_stamped_policy(conn, asset_kind="stem_bundle", mode="stream")
    finally:
        conn.close()

    voc = tmp_path / "v.wav"
    ins = tmp_path / "i.wav"
    _write_wav(voc)
    _write_wav(ins)
    with pytest.raises(HydrationError, match="stream"):
        register_stems.register_pair(
            stable_id="sid-stream",
            vocals=voc,
            instrumental=ins,
            model_name="m",
            model_version="v",
            source_path=str(voc),
            storage=register_stems.RegisterPairStorage(
                root=data_dir / "state" / "stems-roformer-spike",
                s3=fake_s3,
                cfg=_test_cfg(),
                conn=state_db.open_rw(db_path),
            ),
        )


def _with_lock_blocks(fn: object) -> tuple[object, set[int]]:
    """``fn``'s parsed source, and the ids of every node inside one of its
    ``with store_writer_lock(...)`` blocks."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))  # type: ignore[arg-type]
    locked: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "store_writer_lock"
            for item in node.items
        ):
            locked |= {id(inner) for inner in ast.walk(node)}
    return tree, locked


def _calls(tree: object, name: str) -> list[object]:
    import ast

    return [
        node
        for node in ast.walk(tree)  # type: ignore[arg-type]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def test_hydrate_fetches_with_no_lock_and_swaps_only_through_the_locked_helper() -> None:
    """[if] stems_sync.hydrate writes a bundle [then] its fetches hold no lock and its only swap is swap_in_hydrated, [else stop].

    The fetch goes into a staging copy no other writer reads, and the bundle
    lock is striped across ids, so holding it for the transfer would stall
    other tracks' hydrates (Codex on #6821). Read from the real function's
    source: running the fetch needs an object-store client, and the tests use
    no stand-in for one (Codex P1 on #6820). The swap's locking is exercised
    for real on ``swap_in_hydrated`` below.

    MUTATION TARGETS: move a fetch under ``store_writer_lock``; call
    ``replace_bundle_dir`` from ``hydrate`` directly.
    """
    tree, locked = _with_lock_blocks(stems_sync.hydrate)
    fetches = _calls(tree, "fetch_asset")
    assert fetches and _calls(tree, "swap_in_hydrated"), "found no fetch or no swap in hydrate: the probe is not reading it"
    assert not [c for c in fetches if id(c) in locked], "hydrate fetches under the bundle lock"
    assert not _calls(tree, "replace_bundle_dir"), "hydrate swaps without the recheck under the lock"
    helper_tree, helper_locked = _with_lock_blocks(stems_sync.swap_in_hydrated)
    swaps = _calls(helper_tree, "replace_bundle_dir")
    assert swaps and all(id(c) in helper_locked for c in swaps), "swap_in_hydrated swaps outside the lock"
    assert _calls(tree, "live_bundle_digest"), "hydrate takes no content digest before the fetch"


def _bundle(path: Path, content: bytes) -> Path:
    path.mkdir(parents=True)
    (path / "manifest.json").write_bytes(content)
    return path


def test_the_hydrate_swap_waits_for_the_bundle_lock(tmp_path: Path) -> None:
    """[if] another writer holds this bundle's lock [then] the hydrate swap waits for it, then lands, [else stop].

    Holds the exact lock file eviction claims (``bundle_lock_path``), with real
    directories and no stand-ins. A lock resolved against the wrong data
    directory, or none at all, lets the swap land under the hold.

    MUTATION TARGET: drop ``store_writer_lock`` from ``swap_in_hydrated``.
    """
    import threading
    import time

    from apps.cloud.stem_upload_queue import exclusive_file_lock
    from apps.stems.bundle_lock import bundle_lock_path

    data_dir = tmp_path / "data"
    live = _bundle(data_dir / "state" / "stems-roformer-spike" / "sid", b"old")
    staging = _bundle(tmp_path / "staging" / "sid", b"fetched")
    before = stems_sync.live_bundle_digest(live)
    swapped: list[bool] = []
    worker = threading.Thread(target=lambda: swapped.append(stems_sync.swap_in_hydrated(staging, live, before)))
    with exclusive_file_lock(bundle_lock_path(data_dir, "sid")):
        worker.start()
        time.sleep(1.0)  # an unlocked swap takes milliseconds
        assert worker.is_alive(), "the hydrate swap ran while the bundle lock was held"
        assert (live / "manifest.json").read_bytes() == b"old"
    worker.join(timeout=60)
    assert swapped == [True] and (live / "manifest.json").read_bytes() == b"fetched", "control: it lands once released"


def test_a_bundle_published_during_the_fetch_is_kept_not_overwritten(tmp_path: Path) -> None:
    """[if] another writer replaced the bundle while hydrate fetched [then] the swap keeps that bundle, [else stop].

    Moving the fetch out of the lock opened this window (Codex on #6820).
    MUTATION TARGET: drop the fingerprint recheck in ``swap_in_hydrated``.
    """
    live = _bundle(tmp_path / "data" / "state" / "stems-roformer-spike" / "sid", b"old")
    staging = _bundle(tmp_path / "staging" / "sid", b"fetched")
    before = stems_sync.live_bundle_digest(live)
    (live / "manifest.json").write_bytes(b"published by another writer, newer")

    assert stems_sync.swap_in_hydrated(staging, live, before) is False
    assert (live / "manifest.json").read_bytes() == b"published by another writer, newer"
    # Control (overshoot): an unchanged bundle is replaced, and a first
    # hydrate (no bundle before, none now) lands too.
    assert stems_sync.swap_in_hydrated(staging, live, stems_sync.live_bundle_digest(live)) is True
    assert (live / "manifest.json").read_bytes() == b"fetched"
    fresh = tmp_path / "data" / "state" / "stems-roformer-spike" / "sid-new"
    staged_new = _bundle(tmp_path / "staging2" / "sid-new", b"first")
    assert stems_sync.swap_in_hydrated(staged_new, fresh, stems_sync.live_bundle_digest(fresh)) is True
    assert (fresh / "manifest.json").read_bytes() == b"first"


def test_a_same_size_same_mtime_republish_during_the_fetch_is_kept(tmp_path: Path) -> None:
    """[if] a republish keeps every name, size and mtime but not the bytes [then] the swap still keeps it, [else stop].

    A coarse-clock filesystem (FAT, HFS+) gives a same-codec CBR re-render of
    a fixed-duration part exactly that shape (Codex P1 on #6821). Reproduced
    with real files: same length bytes, mtime set back to the old stamp.
    MUTATION TARGET: compare ``current_fingerprint`` (names, sizes, mtimes).
    """
    import os

    from apps.cloud.stem_bundles import current_fingerprint

    live = _bundle(tmp_path / "data" / "state" / "stems-roformer-spike" / "sid", b"render-1")
    part = live / "vocals.mp3"
    part.write_bytes(b"A" * 4096)
    stamps = {p: p.stat().st_mtime_ns for p in (part, live / "manifest.json")}
    before = stems_sync.live_bundle_digest(live)
    metadata_before = current_fingerprint(live)
    part.write_bytes(b"B" * 4096)
    (live / "manifest.json").write_bytes(b"render-2")
    for path, mtime_ns in stamps.items():
        os.utime(path, ns=(mtime_ns, mtime_ns))
    # The probe is asking the right question: by metadata alone nothing moved.
    assert current_fingerprint(live) == metadata_before, "the republish changed metadata: this case is not reproduced"

    staging = _bundle(tmp_path / "staging" / "sid", b"stale cloud copy")
    assert stems_sync.swap_in_hydrated(staging, live, before) is False
    assert part.read_bytes() == b"B" * 4096
    # Control (overshoot): with the bytes unchanged since ``before``, it swaps.
    assert stems_sync.swap_in_hydrated(staging, live, stems_sync.live_bundle_digest(live)) is True
    assert (live / "manifest.json").read_bytes() == b"stale cloud copy"




def _refuse_rmtree_under(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    """The filesystem refuses to remove any tree whose path names ``marker``."""
    real = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if marker in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)


def test_a_hydrate_staging_that_cannot_be_removed_is_reported_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_s3: InMemoryAssetS3
) -> None:
    """[if] a failed hydrate's staging cannot be removed [then] the error names it and keeps the cause, [else stop].

    MUTATION TARGET: ``shutil.rmtree(staging_parent, ignore_errors=True)``,
    the pre-fix cleanup, which returned the fetch error and left the
    staging tree on disk unseen (Sol on #6820). Control: with a removable
    staging tree the fetch's own error is raised and nothing is left.
    """
    from apps.stems.bundle_lock import StagingCleanupError

    cfg = _test_cfg()
    data_dir = tmp_path / "data"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": 3, "files": {"vocals": "vocals.wav"}, "files_sha256": {"vocals": "f" * 64}}),
        encoding="utf-8",
    )
    use_cloud_mode(monkeypatch)
    monkeypatch.setattr("apps.lyrics.stems_sync.asset_clients_for_mode", lambda writing=True: (fake_s3, cfg))
    staging_root = data_dir / "state" / stems_sync.HYDRATE_STAGING_DIR_NAME

    with pytest.raises(asset_store.AssetStoreError):
        stems_sync.hydrate("sid-gone", manifest_path=manifest, data_dir=data_dir)
    assert list(staging_root.iterdir()) == [], "control: a removable staging tree is removed"

    _refuse_rmtree_under(monkeypatch, stems_sync.HYDRATE_STAGING_DIR_NAME)
    with pytest.raises(StagingCleanupError) as raised:
        stems_sync.hydrate("sid-gone", manifest_path=manifest, data_dir=data_dir)
    [stuck] = list(staging_root.iterdir())
    assert str(stuck) in str(raised.value)
    chain: list[BaseException] = []
    error: BaseException | None = raised.value
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(e, asset_store.AssetStoreError) for e in chain), "the fetch's own error is kept"


def _lock_is_free(bundle_dir: Path) -> bool:
    """True when another holder can take ``bundle_dir``'s lock within a few seconds."""
    import threading

    from apps.stems.bundle_lock import bundle_lock

    taken = threading.Event()

    def take() -> None:
        with bundle_lock(bundle_dir):
            taken.set()

    probe = threading.Thread(target=take, daemon=True)
    probe.start()
    free = taken.wait(timeout=5)
    probe.join(timeout=60)
    return free


def _hash_then(monkeypatch: pytest.MonkeyPatch, after: object) -> None:
    """Run ``after(bundle_dir)`` right after each real content hash returns."""
    real = stems_sync.bundle_fingerprint

    def hash_then(bundle_dir: Path) -> str:
        digest = real(bundle_dir)
        after(bundle_dir)  # type: ignore[operator]
        return digest

    monkeypatch.setattr(stems_sync, "bundle_fingerprint", hash_then)


def test_the_hydrate_hashes_run_with_the_bundle_lock_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the hydrate hashes an existing bundle [then] the stripe lock is free while it reads, [else stop].

    The lock is striped 256 ways, so a full read under it stalls other ids'
    hydrates and publishes for a whole disk read (Codex P2 on #6821). Probed
    with a second holder of the real lock while each real hash runs.
    MUTATION TARGET: hash inside ``store_writer_lock`` in either function.
    """
    live = _bundle(tmp_path / "data" / "state" / "stems-roformer-spike" / "sid", b"old")
    staging = _bundle(tmp_path / "staging" / "sid", b"fetched")
    free: list[bool] = []
    _hash_then(monkeypatch, lambda bundle_dir: free.append(_lock_is_free(bundle_dir)))

    before = stems_sync.live_bundle_digest(live)
    assert stems_sync.swap_in_hydrated(staging, live, before) is True

    assert free == [True, True], "a hydrate hash ran while the bundle lock was held"
    assert (live / "manifest.json").read_bytes() == b"fetched"


def test_control_the_lock_probe_sees_a_held_lock(tmp_path: Path) -> None:
    from apps.stems.bundle_lock import bundle_lock

    live = _bundle(tmp_path / "data" / "state" / "stems-roformer-spike" / "sid", b"old")
    with bundle_lock(live):
        assert not _lock_is_free(live)
    assert _lock_is_free(live)


def test_a_bundle_replaced_while_the_swap_hashes_it_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] another writer swaps a bundle in while the unlocked hash reads the old one [then] it is kept, [else stop].

    The hash matched ``before`` because it read the old bytes; only the marks
    re-checked under the lock see the new generation.
    MUTATION TARGET: drop the ``file_marks`` re-check in ``swap_in_hydrated``.
    """
    from apps.stems.bundle_lock import replace_bundle_dir, store_writer_lock

    live = _bundle(tmp_path / "data" / "state" / "stems-roformer-spike" / "sid", b"old")
    staging = _bundle(tmp_path / "staging" / "sid", b"stale cloud copy")
    before = stems_sync.live_bundle_digest(live)
    published = _bundle(tmp_path / "other-writer" / "sid", b"published by another writer, newer")

    def another_writer_publishes(bundle_dir: Path) -> None:
        with store_writer_lock(bundle_dir):
            replace_bundle_dir(published, bundle_dir)
        monkeypatch.undo()

    _hash_then(monkeypatch, another_writer_publishes)
    assert stems_sync.swap_in_hydrated(staging, live, before) is False
    assert (live / "manifest.json").read_bytes() == b"published by another writer, newer"
    # Control (overshoot): with nothing racing, the same swap lands.
    assert stems_sync.swap_in_hydrated(staging, live, stems_sync.live_bundle_digest(live)) is True
    assert (live / "manifest.json").read_bytes() == b"stale cloud copy"


def test_a_digest_taken_while_another_writer_swaps_never_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the bundle is replaced while live_bundle_digest hashes it [then] the digest matches nothing, [else stop].

    MUTATION TARGET: drop the ``file_marks`` re-check in ``live_bundle_digest``.
    """
    live = _bundle(tmp_path / "data" / "state" / "stems-roformer-spike" / "sid", b"old")
    published = _bundle(tmp_path / "other-writer" / "sid", b"newer")

    def another_writer_publishes(bundle_dir: Path) -> None:
        from apps.stems.bundle_lock import replace_bundle_dir, store_writer_lock

        with store_writer_lock(bundle_dir):
            replace_bundle_dir(published, bundle_dir)
        monkeypatch.undo()

    _hash_then(monkeypatch, another_writer_publishes)
    racing = stems_sync.live_bundle_digest(live)
    assert racing == stems_sync.CHANGING
    staging = _bundle(tmp_path / "staging" / "sid", b"stale cloud copy")
    assert stems_sync.swap_in_hydrated(staging, live, racing) is False
    assert (live / "manifest.json").read_bytes() == b"newer"
    # Control (overshoot): a digest taken with nothing racing is the content's.
    assert stems_sync.live_bundle_digest(live) != stems_sync.CHANGING
