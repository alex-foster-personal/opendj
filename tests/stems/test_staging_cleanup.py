"""Writers that stage a stem bundle report staging they cannot remove (STEM-58).

[if] a farm import's staging cannot be removed [then] the error names it and keeps the cause, [else stop].
[if] a rejected RoFormer bundle cannot be removed [then] the error names it and keeps the cause, [else stop].
[if] a stem worker's staging cannot be removed [then] the error names it and keeps the cause, [else stop].

Every one of these used ``ignore_errors=True``, which returned success or the
writer's own error while a whole bundle stayed on disk where nothing sweeps it
(Sol on #6820). The real writers run on real files; the only double is the
filesystem refusing ``rmtree`` for the one path under test.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
from pathlib import Path

import pytest

from apps.lyrics import register_stems
from apps.stems import farm_import
from apps.stems.bundle_lock import StagingCleanupError

pytestmark = [pytest.mark.requirement("STEM-58")]


def _refuse_rmtree_of(monkeypatch: pytest.MonkeyPatch, refused: Path) -> None:
    real = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path) == refused:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)


def _chain(error: BaseException | None) -> list[BaseException]:
    chain: list[BaseException] = []
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    return chain


def _junk_tar(path: Path) -> Path:
    body = b"not a stem bundle"
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo("junk.txt")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return path


def test_a_farm_import_staging_that_cannot_be_removed_is_reported_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: ``shutil.rmtree(staging_parent, ignore_errors=True)`` (the pre-fix cleanup)."""
    stems = tmp_path / "data" / "state" / "stems"
    stems.mkdir(parents=True)
    tar_path = _junk_tar(tmp_path / "sid.tar")
    staging_parent = stems.parent / farm_import.STAGING_DIR_NAME / str(os.getpid())

    with pytest.raises(Exception) as plain:
        farm_import.import_tar(tar_path, stems)
    assert not isinstance(plain.value, StagingCleanupError)
    assert not staging_parent.exists(), "control: a removable staging tree is removed"
    assert not (stems / "sid").exists()

    _refuse_rmtree_of(monkeypatch, staging_parent)
    with pytest.raises(StagingCleanupError) as raised:
        farm_import.import_tar(tar_path, stems)
    assert str(staging_parent) in str(raised.value)
    assert any(type(e) is type(plain.value) for e in _chain(raised.value)), "the import's own error is kept"
    assert not (stems / "sid").exists(), "nothing was published"


def test_a_rejected_roformer_bundle_that_cannot_be_removed_is_reported_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: ``shutil.rmtree(out_dir, ignore_errors=True)`` (the pre-fix cleanup).

    The staged bundle lands at the live path, the strict reader refuses it,
    and its removal fails: left silent, readers would see a bundle the
    register reported as unpublished.
    """
    root = tmp_path / "root"
    root.mkdir()

    def staged(name: str) -> Path:
        tmp_dir = root / f"sid.tmp-{name}"
        tmp_dir.mkdir()
        (tmp_dir / "manifest.json").write_text(json.dumps({"schema_version": 3}), encoding="utf-8")
        return tmp_dir

    out_dir = root / "sid"
    with pytest.raises(Exception) as plain:
        register_stems._publish_staged(staged("a"), out_dir)
    assert not isinstance(plain.value, StagingCleanupError)
    assert not out_dir.exists(), "control: a removable rejected bundle is removed"

    _refuse_rmtree_of(monkeypatch, out_dir)
    with pytest.raises(StagingCleanupError) as raised:
        register_stems._publish_staged(staged("b"), out_dir)
    assert str(out_dir) in str(raised.value)
    assert any(type(e) is type(plain.value) for e in _chain(raised.value)), "the strict reader's error is kept"


@pytest.mark.parametrize("has_filter", [False, True], ids=["python-3.11.0-3", "python-3.11.4+"])
def test_a_farm_import_without_the_tar_data_filter_is_refused_before_unpacking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], has_filter: bool
) -> None:
    """[if] the interpreter has no ``tarfile.data_filter`` [then] preflight fails and nothing is unpacked, [else] preflight passes.

    ``requires-python`` admits 3.11.0 to 3.11.3, whose ``extractall`` takes
    no ``filter`` and raised ``TypeError`` after the pull, with the farm VM
    still running (Codex on #6821). MUTATION TARGETS: drop the check (the
    no-filter row reaches ``extractall``), or refuse every interpreter (the
    control row fails).
    """
    if not has_filter:
        monkeypatch.delattr(tarfile, "data_filter")
    stems = tmp_path / "data" / "state" / "stems"
    stems.mkdir(parents=True)
    tar_path = _junk_tar(tmp_path / "sid.tar")

    code = farm_import.main(["--preflight"])

    if has_filter:
        assert code == 0
        return
    assert code == 1
    assert "3.11.4" in capsys.readouterr().err
    with pytest.raises(farm_import.UnsafeTarExtractionError):
        farm_import.import_tar(tar_path, stems)
    assert not (stems.parent / farm_import.STAGING_DIR_NAME).exists(), "refused before any staging was made"


def test_the_pull_down_script_asks_the_importer_before_it_pulls() -> None:
    """[if] the preflight moves after the pull [then] a refusal leaves the VM running, [else stop]."""
    script = (Path(__file__).resolve().parents[2] / "scripts" / "stem_farm_watch_pull_down.sh").read_text()
    preflight = script.index("farm_import --preflight")
    assert preflight < script.index('"$SCP_BIN"')
    assert preflight < script.index("instances delete")


def test_a_stem_worker_staging_that_cannot_be_removed_is_reported_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION TARGET: ``shutil.rmtree(tmp, ignore_errors=True)`` (the pre-fix cleanup).

    The worker's own staging context runs for real around a write that fails
    part-way, as an encode or publish error would. Control: a removable
    staging tree is removed and the write's own error is what surfaces.
    """
    from scripts.stem_bundle_worker import staged_bundle

    out_dir = tmp_path / "data" / "state" / "stems" / "sid"
    out_dir.parent.mkdir(parents=True)

    class EncodeFailed(RuntimeError):
        pass

    def failing_write() -> Path:
        with staged_bundle(out_dir, "sid") as tmp:
            (tmp / "vocals.mp3").write_bytes(b"half a part")
            failing_write.staging = tmp  # type: ignore[attr-defined]
            raise EncodeFailed("encoder died")

    with pytest.raises(EncodeFailed):
        failing_write()
    assert not failing_write.staging.exists(), "control: a removable staging tree is removed"  # type: ignore[attr-defined]

    real = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path).name.startswith(".stem-sid-"):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    with pytest.raises(StagingCleanupError) as raised:
        failing_write()
    staging = failing_write.staging  # type: ignore[attr-defined]
    assert staging.is_dir(), "control: the refused staging really stayed"
    assert str(staging) in str(raised.value)
    assert any(isinstance(e, EncodeFailed) for e in _chain(raised.value)), "the write's own error is kept"
    assert not out_dir.exists(), "nothing was published"
