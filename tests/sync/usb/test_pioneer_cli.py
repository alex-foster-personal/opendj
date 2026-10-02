"""CLI tests for ``python -m apps.sync.usb.pioneer`` (CAT-06).

Covers:

* ``--help`` for the package entry point + each subcommand.
* ``read`` + ``read --validate`` exit codes against the committed
  ``tests/fixtures/rb-usb-export/`` fixture.
* ``write`` dry-run (no ``sqlcipher3`` required) spec parsing + plan JSON.
* ``write`` spec-parse error exit codes (2 = bad args / parse error).
* ``write --apply`` round-trip via the OneLibrary writer (skipped when
  ``sqlcipher3`` is missing).
* ``write`` alias module ``apps.sync.usb.pioneer.writer``.

All tests are read-only against ``tests/fixtures/``. Output is written
to ``tmp_path`` only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from apps.sync.usb.pioneer.__main__ import (
    _parse_playlist_spec,
    _parse_track_spec,
    main,
)
from apps.sync.usb.pioneer.writer_onelibrary import (
    WRITER_AVAILABLE,
    WRITER_IMPORT_ERROR,
)
from tests.fixtures.conftest import resolve_required_fixture

# Live-write MECHANICS against tmp fixtures: runs with the one-way rekordbox
# import gate ON (root conftest reads the marker). Never a real rb target.
pytestmark = [pytest.mark.requirement("CAT-06"), pytest.mark.rekordbox_writeback]


REPO_ROOT = Path(__file__).resolve().parents[3]


def _fixture_pioneer() -> Path:
    """Resolve ``tests/fixtures/rb-usb-export/PIONEER`` lazily.

    Deferred out of a module-level constant into the fixture-dependent
    test bodies below (rather than resolved once at import time) so an
    ``MDT_ALLOW_MISSING_FIXTURES=1`` skip -- or a missing host with no
    opt-out, which fails closed via ``resolve_required_fixture`` -- drops
    only the tests that actually need USB data. TestSpecParsing, TestHelp,
    and TestWriteDryRun need no fixture and must stay collectible either
    way, since a module-level ``pytest.skip(..., allow_module_level=True)``
    would otherwise skip the whole file as collateral (PR #718 review).
    """
    return resolve_required_fixture("rb-usb-export") / "PIONEER"


def _fixture_onelibrary() -> Path:
    return _fixture_pioneer() / "rekordbox" / "exportLibrary.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_onelibrary_copy(tmp_path: Path) -> Path:
    """Per-test copy of the encrypted OneLibrary fixture.

    The writer opens templates in read/write mode and spawns ``-shm``/``-wal``
    sidecars. Copying to ``tmp_path`` keeps the committed fixture
    untouched.
    """
    import shutil

    dest = tmp_path / "template" / "exportLibrary.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_fixture_onelibrary(), dest)
    return dest


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


class TestSpecParsing:
    def test_playlist_spec_happy(self) -> None:
        assert _parse_playlist_spec("Warmup:1,2,3") == ("Warmup", [1, 2, 3])

    def test_playlist_spec_empty_ids(self) -> None:
        assert _parse_playlist_spec("Empty:") == ("Empty", [])

    def test_playlist_spec_whitespace(self) -> None:
        assert _parse_playlist_spec("  Name  : 10 , 20 ") == ("Name", [10, 20])

    def test_playlist_spec_name_with_colon(self) -> None:
        # rpartition splits on the rightmost ``:`` so names may contain
        # colons -- e.g. a playlist called "Warmup: House" is fine.
        assert _parse_playlist_spec("Warmup: House:5,6") == ("Warmup: House", [5, 6])

    def test_playlist_spec_missing_colon(self) -> None:
        with pytest.raises(ValueError, match="must contain ':'"):
            _parse_playlist_spec("NoColon")

    def test_playlist_spec_empty_name(self) -> None:
        with pytest.raises(ValueError, match="name is empty"):
            _parse_playlist_spec(":1,2")

    def test_playlist_spec_non_int_id(self) -> None:
        with pytest.raises(ValueError, match="track ids must be integers"):
            _parse_playlist_spec("Bad:1,notanint")

    def test_track_spec_happy(self) -> None:
        tid, overlay = _parse_track_spec("7:title=Hello,rating=5,bpmx100=12800")
        assert tid == 7
        assert overlay == {"title": "Hello", "rating": 5, "bpmx100": 12800}

    def test_track_spec_missing_colon(self) -> None:
        with pytest.raises(ValueError, match="must contain ':'"):
            _parse_track_spec("nocolon")

    def test_track_spec_non_int_id(self) -> None:
        with pytest.raises(ValueError, match="track id must be an integer"):
            _parse_track_spec("abc:title=x")

    def test_track_spec_bad_field_shape(self) -> None:
        with pytest.raises(ValueError, match="FIELD=VALUE"):
            _parse_track_spec("1:nothingequals")

    def test_track_spec_unknown_field(self) -> None:
        with pytest.raises(ValueError, match="unknown track field"):
            _parse_track_spec("1:unknown=x")

    def test_track_spec_int_coercion_failure(self) -> None:
        with pytest.raises(ValueError, match="must be an integer"):
            _parse_track_spec("1:rating=notanint")

    def test_track_spec_empty_overlay(self) -> None:
        assert _parse_track_spec("42:") == (42, {})


# ---------------------------------------------------------------------------
# --help behaviour
# ---------------------------------------------------------------------------


class TestHelp:
    def test_package_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "read" in out
        assert "write" in out
        assert "agent-export" in out

    def test_read_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["read", "--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--validate" in out

    def test_write_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["write", "--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--template" in out
        assert "--output" in out
        assert "--apply" in out

    def test_no_subcommand(self, capsys: pytest.CaptureFixture[str]) -> None:
        # argparse sets exit status 2 for missing required subcommand.
        with pytest.raises(SystemExit) as excinfo:
            main([])
        assert excinfo.value.code == 2

    def test_writer_alias_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        from apps.sync.usb.pioneer.writer import main as writer_main

        with pytest.raises(SystemExit) as excinfo:
            writer_main(["--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--template" in out


# ---------------------------------------------------------------------------
# ``read`` subcommand
# ---------------------------------------------------------------------------


class TestRead:
    def test_read_prints_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["read", str(_fixture_pioneer())])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        # The 199-track real export is what the reader asserts elsewhere
        # -- here we just confirm the CLI path is wired.
        assert payload["metadata"]["total_tracks"] > 0
        assert "validation" not in payload

    def test_read_with_validate_ok(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["read", str(_fixture_pioneer()), "--validate"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["validation"]["ok"] is True
        assert payload["validation"]["errors"] == []


# ---------------------------------------------------------------------------
# ``write`` dry-run (no sqlcipher3 required)
# ---------------------------------------------------------------------------


class TestWriteDryRun:
    def test_dry_run_plan_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        template = tmp_path / "template" / "exportLibrary.db"
        template.parent.mkdir(parents=True)
        template.write_bytes(b"not really a sqlcipher file")
        output = tmp_path / "out" / "exportLibrary.db"
        rc = main(
            [
                "write",
                "--template",
                str(template),
                "--output",
                str(output),
                "--playlist",
                "Warmup:1,2,3",
                "--track",
                "1:title=New,rating=5",
            ]
        )
        assert rc == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["mode"] == "dry-run"
        assert Path(plan["template"]) == template.resolve()
        assert Path(plan["output"]) == output.resolve()
        assert plan["playlists"] == [{"name": "Warmup", "track_ids": [1, 2, 3]}]
        assert plan["track_updates"] == [
            {"id": 1, "overlay": {"title": "New", "rating": 5}}
        ]

    def test_dry_run_empty_overlays(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        template = tmp_path / "template.db"
        template.write_bytes(b"x")
        output = tmp_path / "out.db"
        rc = main(
            [
                "write",
                "--template",
                str(template),
                "--output",
                str(output),
            ]
        )
        assert rc == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["playlists"] == []
        assert plan["track_updates"] == []

    def test_dry_run_bad_playlist_spec_rc2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            [
                "write",
                "--template",
                str(tmp_path / "t.db"),
                "--output",
                str(tmp_path / "o.db"),
                "--playlist",
                "BadSpecNoColon",
            ]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "must contain ':'" in err

    def test_dry_run_bad_track_spec_rc2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            [
                "write",
                "--template",
                str(tmp_path / "t.db"),
                "--output",
                str(tmp_path / "o.db"),
                "--track",
                "1:unknown=x",
            ]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown track field" in err


# ---------------------------------------------------------------------------
# ``write --apply`` round-trip (requires sqlcipher3 + fixture)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not WRITER_AVAILABLE,
    reason=f"OneLibrary writer unavailable ({WRITER_IMPORT_ERROR}).",
)
class TestWriteApply:
    def test_apply_round_trip(
        self,
        fixture_onelibrary_copy: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        output = tmp_path / "out" / "exportLibrary.db"
        rc = main(
            [
                "write",
                "--template",
                str(fixture_onelibrary_copy),
                "--output",
                str(output),
                "--playlist",
                "CLI Smoke:1,2,3",
                "--track",
                "1:title=CLI Title,rating=4,bpmx100=12500",
                "--apply",
            ]
        )
        assert rc == 0
        assert output.is_file()
        summary = json.loads(capsys.readouterr().out)
        assert summary["mode"] == "apply"
        assert summary["tracks_updated"] == 1
        assert summary["playlists_written"] == 1
        assert summary["output_size_bytes"] > 0
        assert Path(summary["output_path"]) == output.resolve()

        # Round-trip check: reopen and confirm the playlist is
        # present with the right name.
        from apps.sync.usb.pioneer.writer_onelibrary import read_playlist_roundtrip

        payload = read_playlist_roundtrip(
            onelibrary_path=output, playlist_id=summary["playlist_ids"][0]
        )
        assert payload["name"] == "CLI Smoke"
        assert [t["id"] for t in payload["tracks"]] == [1, 2, 3]

    def test_apply_template_equals_output_rc4(
        self,
        fixture_onelibrary_copy: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # writer_onelibrary refuses to open the same path twice; CLI maps that
        # to exit code 4 (writer failure).
        rc = main(
            [
                "write",
                "--template",
                str(fixture_onelibrary_copy),
                "--output",
                str(fixture_onelibrary_copy),
                "--apply",
            ]
        )
        assert rc == 4
        err = capsys.readouterr().err
        assert "OneLibrary write failed" in err


# ---------------------------------------------------------------------------
# ``write --apply`` writer-missing error path
# ---------------------------------------------------------------------------


def test_apply_writer_import_failure_rc4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Force the writer_onelibrary import in ``_cmd_write`` to fail and verify
    we surface exit code 4 with a clear error message.

    We achieve this by blocking the import at sys.modules + meta-path
    level: register a loader that raises ImportError for
    ``apps.sync.usb.pioneer.writer_onelibrary``.
    """
    import importlib

    # Evict any cached copy so the fresh import inside _cmd_write fails.
    monkeypatch.delitem(
        sys.modules, "apps.sync.usb.pioneer.writer_onelibrary", raising=False
    )

    real_import_module = importlib.import_module

    def blocked_import(name: str, package: str | None = None):  # type: ignore[override]
        if name in {".writer_onelibrary", "apps.sync.usb.pioneer.writer_onelibrary"}:
            raise ImportError("writer_onelibrary blocked for test")
        return real_import_module(name, package)

    # Patch the ``from .writer_onelibrary import ...`` inside ``_cmd_write`` by
    # inserting a dummy module that raises on attribute access.
    class _RaisingModule:
        def __getattr__(self, attr: str):
            raise ImportError("writer_onelibrary blocked for test")

    monkeypatch.setitem(
        sys.modules, "apps.sync.usb.pioneer.writer_onelibrary", _RaisingModule()  # type: ignore[arg-type]
    )

    rc = main(
        [
            "write",
            "--template",
            str(tmp_path / "t.db"),
            "--output",
            str(tmp_path / "o.db"),
            "--apply",
        ]
    )
    assert rc == 4
    err = capsys.readouterr().err
    assert "writer_onelibrary import failed" in err
