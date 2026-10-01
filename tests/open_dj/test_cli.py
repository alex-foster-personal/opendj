"""Tests for :mod:`apps.open_dj.cli` -- argparse entrypoint."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from apps.open_dj.cli import main


@pytest.mark.requirement("OPEN-03")
class TestValidate:
    """`open-dj-tool validate` exit codes: 0 valid, 2 invalid, 1 IO/parse."""

    def test_valid_doc_exits_0(self, fixtures_dir: Path) -> None:
        rc = main(["validate", str(fixtures_dir / "minimal.open-dj.json")])
        assert rc == 0

    def test_invalid_doc_exits_2(self, fixtures_dir: Path) -> None:
        rc = main(["validate", str(fixtures_dir / "invalid_wrapped_title.open-dj.json")])
        assert rc == 2

    def test_missing_file_exits_1(self, tmp_path: Path) -> None:
        rc = main(["validate", str(tmp_path / "does_not_exist.json")])
        assert rc == 1

    def test_malformed_json_exits_1(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not json at all {")
        rc = main(["validate", str(bad)])
        assert rc == 1


@pytest.mark.requirement("OPEN-03")
class TestCanon:
    """`open-dj-tool canon` rewrites to JCS bytes; in-place or stdout."""

    def test_canon_file_to_stdout(
        self, fixtures_dir: Path, capsysbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        rc = main(["canon", str(fixtures_dir / "minimal.open-dj.json")])
        assert rc == 0
        captured = capsysbinary.readouterr()
        expected = (fixtures_dir / "minimal.open-dj.json").read_bytes()
        assert captured.out == expected

    def test_canon_in_place_rewrites_file(
        self, fixtures_dir: Path, tmp_path: Path
    ) -> None:
        """Non-canonical input file becomes canonical after in-place canon."""
        doc = json.loads((fixtures_dir / "minimal.open-dj.json").read_bytes())
        target = tmp_path / "pretty.open-dj.json"
        # Write pretty (non-canonical) -- json.dumps inserts spaces + sorts=False.
        target.write_text(json.dumps(doc, indent=2))
        rc = main(["canon", "--in-place", str(target)])
        assert rc == 0
        canon_bytes = target.read_bytes()
        expected = (fixtures_dir / "minimal.open-dj.json").read_bytes()
        assert canon_bytes == expected

    def test_canon_stdin(
        self, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        raw = (fixtures_dir / "minimal.open-dj.json").read_bytes()
        # Build a fake stdin with a .buffer attribute since cli reads
        # sys.stdin.buffer.
        class _FakeStdin:
            buffer = io.BytesIO(raw)
        monkeypatch.setattr(sys, "stdin", _FakeStdin())
        rc = main(["canon", "-"])
        assert rc == 0
        out = capsysbinary.readouterr().out
        assert out == raw

    def test_canon_missing_file_exits_1(self, tmp_path: Path) -> None:
        rc = main(["canon", str(tmp_path / "missing.json")])
        assert rc == 1


@pytest.mark.requirement("OPEN-03")
class TestDiff:
    """`open-dj-tool diff` exit codes: 0 identical, 1 differ, 2 IO error."""

    def test_identical_docs_exit_0(self, fixtures_dir: Path) -> None:
        path = str(fixtures_dir / "minimal.open-dj.json")
        rc = main(["diff", path, path])
        assert rc == 0

    def test_different_docs_exit_1(
        self, fixtures_dir: Path, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Make a diverged copy of minimal with a different title.
        doc = json.loads((fixtures_dir / "minimal.open-dj.json").read_bytes())
        doc["tracks"][0]["title"] = "Different"
        other = tmp_path / "other.open-dj.json"
        other.write_text(json.dumps(doc))
        rc = main(["diff",
                   str(fixtures_dir / "minimal.open-dj.json"),
                   str(other)])
        assert rc == 1
        captured = capsys.readouterr()
        assert "title" in captured.out

    def test_missing_file_exit_2(self, fixtures_dir: Path, tmp_path: Path) -> None:
        rc = main(["diff",
                   str(fixtures_dir / "minimal.open-dj.json"),
                   str(tmp_path / "nope.json")])
        assert rc == 2


# REQ: OPEN-03
# REQ: OPEN-03a
@pytest.mark.requirement("OPEN-03")
def test_no_subcommand_prints_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "usage" in err.lower()
