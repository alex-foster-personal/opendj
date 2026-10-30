"""Writers that verify a bundle after its rename hold readers off until it passes or is gone (STEM-58).

[if] a hydrated bundle fails its strict re-verify [then] no deck read sees it before it is removed, [else stop].
[if] a RoFormer bundle fails its strict read back [then] no deck read sees it before it is removed, [else stop].

The RoFormer register renames the staged bundle to its live path and only
then runs the strict reader, so it holds the read barrier until the read back
settles; a deck hydration verifies its fetched copy before swapping it in.
Before, the verify and the removal of a refused bundle ran outside the read
barrier, so a deck read landing in between served a bundle the writer was
about to take away (Sol on #6820). The writers, the verify and the read
barrier are all real; the only wrapper is a probe that starts a real reader
while the verify runs and records what that reader found.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.cloud import stem_cache_budget, stem_hydration
from apps.cloud.asset_store import asset_object_key
from apps.cloud.config import CloudConfig
from apps.cloud.stem_source import DirectR2Source
from apps.lyrics import register_stems
from tests.cloudsync.conftest import InMemoryAssetS3
from tests.cloudsync.test_stem_hydration import _manifest_bytes, _wav_bytes

pytestmark = [pytest.mark.requirement("STEM-58")]


def _probe_reads_during(
    module: object, monkeypatch: pytest.MonkeyPatch, data_dir: Path, bundle: Path
) -> tuple[list[bool], list[threading.Thread]]:
    """Wrap ``module.load_stem_bundle`` so a real reader starts while it runs.

    The reader takes the deck read barrier and records whether a bundle was
    at the live path when it got in. The verify itself is the real one.
    """
    real: Callable[..., object] = module.load_stem_bundle  # type: ignore[attr-defined]
    saw_bundle: list[bool] = []
    readers: list[threading.Thread] = []

    def read() -> None:
        with stem_cache_budget.read_barrier(data_dir, bundle.name):
            saw_bundle.append(bundle.exists())

    def probing_load(*args: object, **kwargs: object) -> object:
        reader = threading.Thread(target=read)
        readers.append(reader)
        reader.start()
        reader.join(0.3)  # time to get in, if nothing holds it off
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "load_stem_bundle", probing_load)
    return saw_bundle, readers


def _seed(fake_s3: InMemoryAssetS3, cfg: CloudConfig, stable_id: str, *, valid: bool) -> dict[str, str]:
    manifest = _manifest_bytes(stable_id)
    if not valid:
        manifest = (json.dumps({**json.loads(manifest), "schema_version": 99}) + "\n").encode()
    files = {
        "manifest.json": manifest,
        **{f"{part}.wav": _wav_bytes() for part in ("vocals", "drums", "bass", "other")},
    }
    entry: dict[str, str] = {}
    for filename, body in files.items():
        digest = hashlib.sha256(body).hexdigest()
        fake_s3.put_object_if_none_match(cfg.audio_bucket, asset_object_key(digest), body)
        entry[filename] = digest
    return entry


@pytest.mark.parametrize("valid", [False, True], ids=["refused", "control-verified"])
def test_a_hydrated_bundle_is_never_read_before_its_verify_settles(
    tmp_path: Path, fake_s3: InMemoryAssetS3, cfg: CloudConfig, monkeypatch: pytest.MonkeyPatch, valid: bool
) -> None:
    """MUTATION TARGET: swap the fetched copy in before its strict verify.

    Every load the hydrate makes starts a real reader. Refused: no reader
    ever finds a bundle at the live path. Control: a bundle that verifies is
    published, and with the swap moved ahead of the verify the refused case
    is what goes red.
    """
    data_dir = tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    sid = "verify-track"
    saw_bundle, readers = _probe_reads_during(stem_hydration, monkeypatch, data_dir, stems_dir / sid)

    outcome = stem_hydration.hydrate_one(
        sid,
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={sid: _seed(fake_s3, cfg, sid, valid=valid)},
        stems_dir=stems_dir,
    )
    for reader in readers:
        reader.join(30)

    assert readers, "control: the probe ran inside the real verify"
    assert outcome.status == ("hydrated" if valid else "error"), outcome
    if not valid:
        assert not any(saw_bundle), "a deck read saw a bundle the hydrate refused"
    assert (stems_dir / sid).is_dir() is valid


@pytest.mark.parametrize("valid", [False, True], ids=["refused", "control-verified"])
def test_a_roformer_bundle_is_never_read_before_its_read_back_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bool
) -> None:
    """MUTATION TARGET: drop ``readers_held_off`` from ``_publish_staged``.

    Refused: the reader gets in only once the bundle is gone. Control: a
    bundle the strict reader accepts is what the reader finds.
    """
    data_dir = tmp_path / "data"
    root = data_dir / "state" / "stems-roformer-spike"
    root.mkdir(parents=True)
    sid = "roformer-track"
    out_dir = root / sid
    staged = root / f"{sid}.tmp-a"
    staged.mkdir()
    manifest = json.loads(_manifest_bytes(sid))
    if not valid:
        manifest["schema_version"] = 99
    (staged / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for part in ("vocals", "drums", "bass", "other"):
        (staged / f"{part}.wav").write_bytes(_wav_bytes())
    saw_bundle, readers = _probe_reads_during(register_stems, monkeypatch, data_dir, out_dir)

    if valid:
        register_stems._publish_staged(staged, out_dir)
    else:
        with pytest.raises(Exception, match="schema_version"):
            register_stems._publish_staged(staged, out_dir)
    for reader in readers:
        reader.join(30)

    assert len(readers) == 1, "control: the probe ran inside the real read back"
    assert saw_bundle == [valid]
    assert out_dir.is_dir() is valid


def test_a_refused_copy_that_cannot_be_handed_back_leaves_no_verify_container(
    tmp_path: Path, fake_s3: InMemoryAssetS3, cfg: CloudConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a refused copy cannot be moved back out of its ``.verify`` container [then] the container is still removed, [else stop].

    The caller discards only the temp dir, so a copy stranded in the
    container sat outside every cache scan and retries could fill the
    volume (Codex on #6821). The only double is the filesystem refusing the
    one rename that hands the copy back. Control: that rename really was
    refused, and the hydrate still reports an error.

    MUTATION TARGET: drop the ``except OSError`` cleanup in ``_publish_fetched``.
    """
    data_dir = tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    sid = "stranded-track"
    real_rename = Path.rename
    refused: list[Path] = []

    def rename(self: Path, target: str | Path) -> Path:
        if self.parent.name.endswith(".verify") and self.name == sid:
            refused.append(self)
            raise PermissionError(32, "sharing violation", str(self))
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    outcome = stem_hydration.hydrate_one(
        sid,
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=fake_s3),
        index={sid: _seed(fake_s3, cfg, sid, valid=False)},
        stems_dir=stems_dir,
    )

    assert refused, "control: the hand-back rename was refused"
    assert outcome.status == "error", outcome
    assert not [p for p in stems_dir.iterdir() if p.name.endswith(".verify")], "a verify container was left behind"
    assert not (stems_dir / sid).exists()


@pytest.mark.parametrize("orphaned", [True, False], ids=["orphaned-backup", "control-no-backup"])
def test_a_hydration_keeps_the_bundle_an_interrupted_swap_left_aside(
    tmp_path: Path, fake_s3: InMemoryAssetS3, cfg: CloudConfig, orphaned: bool
) -> None:
    """[if] a writer died between a swap's two renames [then] a hydration puts its backup back and keeps it, [else stop].

    A background hydrate has no deck-route recovery in front of it, so the
    swap restored the backup only to replace it with the fetched copy and
    delete it as the old bundle: a render never uploaded was lost (Codex P1
    on #6821). The local-only render is marked by a file R2 does not have.
    Control: with no backup the fetched copy is published as before.

    MUTATION TARGET: drop the restore calls ahead of the keep check in ``_publish_fetched``.
    """
    from apps.stems.bundle_lock import swap_backup_path

    data_dir = tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    sid = "interrupted-track"
    index = {sid: _seed(fake_s3, cfg, sid, valid=True)}
    source = DirectR2Source(cfg=cfg, s3=fake_s3)

    def hydrate() -> stem_hydration.HydrationOutcome:
        return stem_hydration.hydrate_one(sid, data_dir=data_dir, source=source, index=index, stems_dir=stems_dir)

    bundle = stems_dir / sid
    if orphaned:
        assert hydrate().status == "hydrated"  # a real, valid bundle to strand
        (bundle / "local-only.txt").write_text("never uploaded", encoding="utf-8")
        backup = swap_backup_path(bundle)
        backup.parent.mkdir(parents=True, exist_ok=True)
        bundle.rename(backup)
        assert not bundle.exists() and backup.is_dir(), "control: the swap was interrupted between its renames"

    outcome = hydrate()

    assert bundle.is_dir()
    if orphaned:
        assert outcome.status == "already_local", outcome
        assert (bundle / "local-only.txt").read_text(encoding="utf-8") == "never uploaded"
        assert not swap_backup_path(bundle).exists()
    else:
        assert outcome.status == "hydrated", outcome
        assert not (bundle / "local-only.txt").exists()
