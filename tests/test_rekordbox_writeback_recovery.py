"""Recovery paths stay OPEN in one-way import mode, and stay hard to abuse.

The gate protects the user's rekordbox library from this app. Refusing the
UNDO for a write that already landed does the opposite: it leaves a bad write
in place with the recovery taken away. So rollback is mapped ``gated=False``
and this module pins both halves of that decision -- it works with the gate
off, and every non-gate rail that makes it un-aimable still fires.

Also here: the two path-shaped ways a caller could point a recovery or replica
write somewhere it does not belong. ``backup_id`` is joined into a path, and a
crate ``--map`` names its own destination.

PATH COMPARISON IS LEXICAL. ``Path.__eq__`` and ``Path.is_relative_to`` collapse
neither ``..`` nor symlinks, so every containment probe below is run in the
awkward forms as well as the obvious one.

Regression lines:
  - if rollback refuses while the gate is off then undo was taken from a user
    who already has a bad write, so broken
  - if rollback stops requiring confirmed=true, or accepts a manifest bound to
    another target or revision, then broken
  - if a backup_id that is not uuid4().hex reaches a path join then broken
  - if a crate --map dest escapes the crate root on ANY transport lane, by
    `..` or by symlink, then broken
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.rekordbox_writeback import (
    REKORDBOX_WRITEBACK_ENABLED_ENV,
    WRITEBACK_DISABLED_CODE,
)


@pytest.fixture(autouse=True)
def _gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here starts from the shipped default: writeback OFF."""
    monkeypatch.delenv(REKORDBOX_WRITEBACK_ENABLED_ENV, raising=False)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from apps.webui.server import rb_vendor
    from apps.webui.server.app import create_app
    from apps.webui.server.backend import InMemoryBackend, Playlist

    monkeypatch.setattr(rb_vendor, "playlist_order_index", dict)
    backend = InMemoryBackend()
    backend.seed_playlist(Playlist(playlist_id="pl-1", name="Warmup", vendor="rekordbox"))
    return TestClient(create_app(backend=backend))


def _never_called_factory(*_args, **_kwargs):
    raise AssertionError("no writer should be opened on this path")


# 03 is recovery staying ungated with every non-gate rail still firing;
# 04 is the crate and ssh containment lanes resolving locally and refusing
# '..' remotely.
pytestmark = [
    pytest.mark.requirement("SYNC-ONEWAY-03"),
    pytest.mark.requirement("SYNC-ONEWAY-04"),
]

VALID_BACKUP_ID = "0" * 32


def _rollback_body(target: Path, backup_id: str = VALID_BACKUP_ID) -> dict:
    return {
        "vendor": "rekordbox", "target_mode": "live", "target_path": str(target),
        "target_id": "t-1", "backup_id": backup_id,
        "expected_target_revision": "rev-1", "confirmed": True,
    }


def test_rollback_is_not_refused_by_the_gate(
    client: TestClient, tmp_path: Path
) -> None:
    """Undo must survive one-way import mode.

    Rollback only exists after a GATED apply already wrote; refusing it would
    leave the user holding a bad write with the undo taken away. This probe
    still fails (503: a tmp path is not the canonical live target) -- what it
    proves is that the one-way gate is not what stopped it.
    """
    response = client.post(
        "/api/v1/playlists/pl-1/writeback/rollback",
        json=_rollback_body(tmp_path / "nope.db"),
    )
    assert response.status_code != 403
    assert response.json().get("error") != WRITEBACK_DISABLED_CODE


def test_rollback_reaches_the_writer_while_the_gate_is_off(tmp_path: Path) -> None:
    """With a legitimate manifest behind it, rollback completes, gate off."""
    from apps.webui.server.playlist_writeback import WritebackService

    seen: dict[str, object] = {}

    class _Writer:
        def restore_backup(self, backup_id, target_id, expected_revision):
            seen.update(backup_id=backup_id, target_id=target_id)
            return "restored-revision"

    @contextmanager
    def _factory(*_args, **_kwargs):
        yield _Writer()

    result = WritebackService(writer_factory=_factory).rollback(
        vendor="rekordbox", target_mode="live", target_path=str(tmp_path / "live.db"),
        target_id="t-1", backup_id=VALID_BACKUP_ID,
        expected_target_revision="rev-1", confirmed=True,
    )
    assert result.rolled_back is True
    assert result.target_revision == "restored-revision"
    assert seen["backup_id"] == VALID_BACKUP_ID


def test_rollback_still_requires_confirmation_while_the_gate_is_off(
    tmp_path: Path,
) -> None:
    """Ungating the gate rail must not have loosened any other rail."""
    from apps.webui.server.playlist_writeback import WritebackConflict, WritebackService

    with pytest.raises(WritebackConflict, match="confirmed=true"):
        WritebackService(writer_factory=_never_called_factory).rollback(
            vendor="rekordbox", target_mode="live",
            target_path=str(tmp_path / "live.db"),
            target_id="t-1", backup_id=VALID_BACKUP_ID,
            expected_target_revision="rev-1", confirmed=False,
        )


@pytest.mark.parametrize(
    "backup_id",
    ["../../../../etc/passwd", "..%2Fescape", "b-1", "", "0" * 31, "0" * 33, "A" * 32],
)
def test_rollback_refuses_a_backup_id_that_is_not_uuid4_hex(
    client: TestClient, tmp_path: Path, backup_id: str
) -> None:
    """It is joined into a path, so ``../`` would read a file of the caller's choosing."""
    response = client.post(
        "/api/v1/playlists/pl-1/writeback/rollback",
        json=_rollback_body(tmp_path / "nope.db", backup_id),
    )
    assert response.status_code == 422


def test_backup_id_is_validated_at_the_path_join_too(tmp_path: Path) -> None:
    """Defence in depth: the 422 is not the only thing standing between
    an attacker-chosen string and a filesystem read."""
    from apps.smartlists import writeback_backup

    with pytest.raises(ValueError, match="32 lowercase hex"):
        writeback_backup.reversal_path("rekordbox", "../../../../etc/passwd")
    with pytest.raises(ValueError, match="32 lowercase hex"):
        writeback_backup.backup_path("rekordbox", "../escape")
    assert writeback_backup.reversal_path("rekordbox", VALID_BACKUP_ID).parent == (
        writeback_backup.WRITEBACK_BACKUP_DIR
    )


def test_rollback_refuses_a_foreign_or_stale_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preimage must bind THIS target and THIS revision, gate or no gate."""
    import json

    from apps.smartlists import writeback_backup

    monkeypatch.setattr(writeback_backup, "WRITEBACK_BACKUP_DIR", tmp_path)
    target = tmp_path / "live.db"
    target.write_bytes(b"")
    payload = {
        "backup_id": VALID_BACKUP_ID, "vendor": "rekordbox",
        "target_path": str(target.resolve()), "target_id": "t-1",
        "stable_preimage": ["s1"], "native_preimage": ["n1"],
        "post_apply_revision": "rev-1",
    }
    path = tmp_path / f"rekordbox.{VALID_BACKUP_ID}.reversal.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    # Same manifest, correct revision: accepted.
    assert writeback_backup.read_reversal(
        "rekordbox", VALID_BACKUP_ID, target, "t-1", "rev-1"
    ) == (["s1"], ["n1"])
    # Stale revision, and a foreign target id: both rejected.
    with pytest.raises(RuntimeError, match="does not bind this exact target"):
        writeback_backup.read_reversal(
            "rekordbox", VALID_BACKUP_ID, target, "t-1", "rev-2"
        )
    with pytest.raises(RuntimeError, match="does not bind this exact target"):
        writeback_backup.read_reversal(
            "rekordbox", VALID_BACKUP_ID, target, "someone-elses-playlist", "rev-1"
        )


# ----- crate destinations cannot escape the replica crate -------------------
#
# Three transport lanes, one containment rule. Each test below drives the LANE
# rather than the helper, because the defect being guarded was never that the
# helper was wrong -- it was that two of the three lanes never called it.


def _crate_and_outside(tmp_path: Path) -> tuple[Path, Path]:
    crate = tmp_path / "crate"
    (crate / "pioneer-share").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    return crate, outside


def _plan_with_dest(source: Path, dest: Path):
    from apps.webui.crate_sync import CrateFile, SyncPlan

    return SyncPlan(
        scope="probe",
        files=(CrateFile(source=source, dest=dest, kind="track", size_bytes=0),),
        skipped_streaming=0,
        skipped_absent=0,
    )


@pytest.fixture
def _share_root_off_the_real_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point SHARE_ROOT at tmp for the duration of a lane probe.

    ``_source_group`` and ``crate_dest`` both resolve SHARE_ROOT. Resolving is
    harmless, but a test in THIS suite must not so much as construct the real
    share path, so the constant is redirected rather than trusted.
    """
    from apps.webui import crate_sync

    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setattr(crate_sync.platform_paths, "SHARE_ROOT", share)
    return share


@pytest.mark.usefixtures("_share_root_off_the_real_share")
def test_a_crate_map_cannot_aim_a_local_copy_outside_the_crate(tmp_path: Path) -> None:
    """``--map FROM=<inside the Pioneer share>`` used to mkdir + copy2 into it."""
    from apps.webui.crate_sync import CrateDestinationEscape, apply_plan

    crate, outside = _crate_and_outside(tmp_path)
    source = tmp_path / "owner" / "a.mp3"
    source.parent.mkdir()
    source.write_bytes(b"")

    # Inside the crate: the lane runs and copies.
    assert apply_plan(
        _plan_with_dest(source, crate / "pioneer-share" / "a.mp3"),
        dest_kind="local", dest_host="", crate_root=crate, user_maps=(),
    ) == 1
    # Outside it, plainly and then by `..`: refused before copy2 is reached.
    for dest in (outside / "a.mp3", crate / ".." / "outside" / "a.mp3"):
        with pytest.raises(CrateDestinationEscape, match="escapes the replica crate"):
            apply_plan(
                _plan_with_dest(source, dest),
                dest_kind="local", dest_host="", crate_root=crate, user_maps=(),
            )


@pytest.mark.usefixtures("_share_root_off_the_real_share")
def test_a_symlinked_crate_dest_cannot_escape_on_the_local_lane(tmp_path: Path) -> None:
    """A symlink is the case a lexical containment check cannot see at all.

    ``Path('/crate/link/a.mp3').is_relative_to('/crate')`` is True however far
    outside the crate ``link`` actually points, so this only passes because the
    local lane resolves both sides.
    """
    from apps.webui.crate_sync import CrateDestinationEscape, apply_plan

    crate, outside = _crate_and_outside(tmp_path)
    (crate / "escape-hatch").symlink_to(outside, target_is_directory=True)
    source = tmp_path / "owner" / "a.mp3"
    source.parent.mkdir()
    source.write_bytes(b"")

    with pytest.raises(CrateDestinationEscape, match="escapes the replica crate"):
        apply_plan(
            _plan_with_dest(source, crate / "escape-hatch" / "a.mp3"),
            dest_kind="local", dest_host="", crate_root=crate, user_maps=(),
        )
    assert not (outside / "a.mp3").exists()


@pytest.mark.usefixtures("_share_root_off_the_real_share")
def test_a_crate_map_cannot_aim_the_ssh_push_lane_outside_the_crate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This lane had NO containment check and mkdir -p's its dest on the remote.

    A remote path cannot be resolved from here, so the rule is the honest
    equivalent: reject `..` outright, then compare normalised paths.
    """
    import subprocess

    from apps.webui import crate_sync

    def _no_subprocess(*_args, **_kwargs):
        raise AssertionError("the escape must be refused before any ssh or rsync runs")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    crate, outside = _crate_and_outside(tmp_path)
    source = tmp_path / "owner" / "a.mp3"
    source.parent.mkdir()
    source.write_bytes(b"")
    maps = ((str(tmp_path / "owner"), str(outside)),)

    with pytest.raises(
        crate_sync.CrateDestinationEscape, match="escapes the replica crate"
    ):
        crate_sync.apply_plan(
            _plan_with_dest(source, outside / "a.mp3"),
            dest_kind="ssh", dest_host="dev@nowhere.invalid",
            crate_root=crate, user_maps=maps,
        )


def test_a_manifest_cannot_aim_the_ssh_pull_lane_outside_the_crate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pull lane writes locally, so its containment resolves both sides."""
    import subprocess

    from apps.webui import crate_sync

    def _no_subprocess(*_args, **_kwargs):
        raise AssertionError("the escape must be refused before any rsync runs")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    key = tmp_path / "fake_key"
    key.write_text("not a key", encoding="utf-8")
    monkeypatch.setattr(crate_sync, "OWNER_SSH_KEY", key)
    crate, outside = _crate_and_outside(tmp_path)
    owner = sorted(crate_sync.ALLOWED_OWNER_SSH)[0]

    def _manifest(dest_root: Path) -> dict:
        return {"files": [{
            "bytes": 0, "kind": "track", "mtime_s": 0, "stable_ids": [],
            "relative": "a.mp3", "source": str(tmp_path / "owner" / "a.mp3"),
            "source_root": str(tmp_path / "owner"),
            "dest": str(dest_root / "a.mp3"), "dest_root": str(dest_root),
        }]}

    with pytest.raises(
        crate_sync.CrateDestinationEscape, match="escapes the replica crate"
    ):
        crate_sync._rsync_manifest_from_host(
            _manifest(outside), owner=owner, crate_root=crate
        )
    # And by symlink, which a lexical check would wave straight through.
    (crate / "escape-hatch").symlink_to(outside, target_is_directory=True)
    with pytest.raises(
        crate_sync.CrateDestinationEscape, match="escapes the replica crate"
    ):
        crate_sync._rsync_manifest_from_host(
            _manifest(crate / "escape-hatch"), owner=owner, crate_root=crate
        )


