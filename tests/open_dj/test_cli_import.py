"""Tests for ``open-dj-tool import`` (Phase 15 CLI finisher).

Live-write paths are never exercised here; we exercise only:

* dry-run default
* typed-confirm refusal
* module-family (rekordbox / djay) live-write refusal with follow-up hint
* class-family (serato / traktor) live write via the real adapter onto a
  tmp_path target (adapter.write() is side-effect-free w.r.t. the user's
  real library because we point it at tmp_path).
* invalid input (bad JSON, schema errors)
* unknown adapter
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.open_dj.cli import main


# ---------------------------------------------------------- fixtures


_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _write_minimal(tmp_path: Path) -> Path:
    """Copy the canonical minimal fixture into tmp_path for mutation."""
    src = tmp_path / "library.open-dj.json"
    src.write_bytes((_FIXTURES_DIR / "minimal.open-dj.json").read_bytes())
    return src


# ---------------------------------------------------------- dry-run


@pytest.mark.requirement("OPEN-01")
class TestImportDryRun:

    def test_dry_run_default_exits_0(self, tmp_path: Path) -> None:
        src = _write_minimal(tmp_path)
        target = tmp_path / "target.nml"
        rc = main(
            [
                "import",
                "--adapter",
                "traktor",
                "--source",
                str(src),
                "--target",
                str(target),
            ]
        )
        assert rc == 0
        # Target must NOT be written in dry-run mode.
        assert not target.exists()

    def test_dry_run_module_adapter_exits_0(self, tmp_path: Path) -> None:
        src = _write_minimal(tmp_path)
        # Point --target at a file we can safely not-create.
        target = tmp_path / "master.db"
        rc = main(
            [
                "import",
                "--adapter",
                "rekordbox",
                "--source",
                str(src),
                "--target",
                str(target),
            ]
        )
        assert rc == 0
        assert not target.exists()


# ---------------------------------------------------------- safety refusal


@pytest.mark.requirement("OPEN-01")
class TestImportSafetyRefusal:

    def test_live_without_typed_confirm_refused(self, tmp_path: Path) -> None:
        src = _write_minimal(tmp_path)
        target = tmp_path / "target.nml"
        rc = main(
            [
                "import",
                "--adapter",
                "traktor",
                "--source",
                str(src),
                "--target",
                str(target),
                "--live",
            ]
        )
        assert rc == 3
        assert not target.exists()

    def test_rekordbox_live_import_refused_with_hint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = _write_minimal(tmp_path)
        target = tmp_path / "master.db"
        target.write_bytes(b"existing db bytes")
        target_before = target.read_bytes()
        rc = main(
            [
                "import",
                "--adapter",
                "rekordbox",
                "--source",
                str(src),
                "--target",
                str(target),
                "--live",
                "--i-understand-the-risks",
            ]
        )
        assert rc == 3
        # Never mutated the target.
        assert target.read_bytes() == target_before
        err = capsys.readouterr().err
        assert "apps.sync.apply_ratings" in err or "apps.sync.playlist_apply" in err


# ---------------------------------------------------------- validation


@pytest.mark.requirement("OPEN-01")
class TestImportInputValidation:

    def test_bad_json_exits_1(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        rc = main(
            [
                "import",
                "--adapter",
                "traktor",
                "--source",
                str(bad),
                "--target",
                str(tmp_path / "x.nml"),
            ]
        )
        assert rc == 1

    def test_schema_invalid_exits_2(self, tmp_path: Path) -> None:
        bad = tmp_path / "invalid.json"
        # Missing mandatory fields: validator returns errors.
        bad.write_bytes(json.dumps({"version": "0.2"}).encode())
        rc = main(
            [
                "import",
                "--adapter",
                "traktor",
                "--source",
                str(bad),
                "--target",
                str(tmp_path / "x.nml"),
            ]
        )
        assert rc == 2

    def test_missing_source_exits_1(self, tmp_path: Path) -> None:
        rc = main(
            [
                "import",
                "--adapter",
                "traktor",
                "--source",
                str(tmp_path / "absent.json"),
                "--target",
                str(tmp_path / "out.nml"),
            ]
        )
        assert rc == 1


# ---------------------------------------------------------- class-family live


@pytest.mark.requirement("OPEN-01")
class TestImportClassFamilyLive:
    """Exercise the real Traktor adapter.write() via the CLI, pointing at
    tmp_path. This *is* a live write, but to a throwaway temp target;
    Native Instruments' actual Traktor install is never touched.
    """

    def test_traktor_live_import_writes_nml(self, tmp_path: Path) -> None:
        # Use the canonical minimal fixture (schema-valid, provenance
        # envelopes, 40-hex track_id). The CLI unwraps those envelopes
        # before handing the library to TraktorAdapter.write().
        src = _write_minimal(tmp_path)
        target = tmp_path / "collection.nml"

        rc = main(
            [
                "import",
                "--adapter",
                "traktor",
                "--source",
                str(src),
                "--target",
                str(target),
                "--live",
                "--i-understand-the-risks",
            ]
        )
        assert rc == 0, "live traktor import to tmp_path should succeed"
        assert target.exists()
        assert target.stat().st_size > 0


# ---------------------------------------------------------- unknown adapter


@pytest.mark.requirement("OPEN-01")
def test_import_unknown_adapter_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "import",
                "--adapter",
                "bogus",
                "--source",
                str(tmp_path / "x.json"),
                "--target",
                str(tmp_path / "y"),
            ]
        )
    assert excinfo.value.code == 2
