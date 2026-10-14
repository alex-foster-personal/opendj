"""The Cmd K index capture still reproduces from the engine (CMDK-04, Codex P2 on #6089).

Re-runs ``tests/webui/capture_cmdk_index.py`` against the engine composition root
and checks the committed fixture is exactly what it writes, so a stored comment
reaching ``GET /tracks/index`` is proven end to end, not assumed from the model.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.webui import capture_cmdk_index

FIXTURES = Path(__file__).resolve().parents[2] / "apps/webui/frontend/tests/unit/fixtures"
GLASS = "3" * 40


def test_capture_reproduces_the_committed_fixture_with_comments(tmp_path: Path) -> None:
    out = tmp_path / "index.json"
    capture_cmdk_index.main(out)
    body = json.loads(out.read_text())
    by_id = {row["stable_id"]: row for row in body["items"]}
    assert by_id[GLASS]["comments"] == capture_cmdk_index.COMMENTS[GLASS]
    assert all(row["comments"] is None for sid, row in by_id.items() if sid != GLASS)
    committed = json.loads((FIXTURES / "cmdk-index-captured.json").read_text())
    # The revision hashes the run's own state, so only the rows must match.
    assert body["items"] == committed["items"]
    manifest = json.loads((FIXTURES / "cmdk-index-captured.manifest.json").read_text())
    raw = (FIXTURES / "cmdk-index-captured.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest["files"]["cmdk-index-captured.json"]["sha256"]
