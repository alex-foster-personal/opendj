"""SMART-01/02 -- smartlists CLI golden tests."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from apps.smartlists.cli import create as cli_create
from apps.smartlists.cli import delete as cli_delete
from apps.smartlists.cli import list as cli_list


pytestmark = pytest.mark.requirement("SMART-02")


FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "smartlists"
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def test_create_from_fixture_json(
    db_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    rc = cli_create.main([
        "--db", str(db_path),
        "--name", "Fresh House",
        "--rule", str(FIXTURE_DIR / "fresh_house.json"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Fresh House" in out
    assert "referenced_fields=['bpm', 'genre']" in out


def test_create_invalid_rule_returns_2(
    db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    bad_rule = tmp_path / "bad.json"
    bad_rule.write_text(json.dumps(
        {"field": "mystery", "op": "=", "value": 1}
    ))
    rc = cli_create.main([
        "--db", str(db_path),
        "--name", "b", "--rule", str(bad_rule),
    ])
    assert rc == 2


def test_list_after_create(db_path: Path) -> None:
    cli_create.main([
        "--db", str(db_path),
        "--name", "Energetic",
        "--rule", str(FIXTURE_DIR / "high_energy.json"),
    ])
    buf = io.StringIO()
    cli_list.main(["--db", str(db_path), "--format", "json"], out=buf)
    data = json.loads(buf.getvalue())
    assert len(data) == 1 and data[0]["name"] == "Energetic"
    assert data[0]["referenced_fields"] == ["energy"]


def test_delete_roundtrip(db_path: Path) -> None:
    cli_create.main([
        "--db", str(db_path),
        "--name", "tmp",
        "--rule", str(FIXTURE_DIR / "high_energy.json"),
    ])
    rc = cli_delete.main(["--db", str(db_path), "--name", "tmp"])
    assert rc == 0
    rc = cli_delete.main(["--db", str(db_path), "--name", "tmp"])
    assert rc == 1
