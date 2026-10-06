"""The index body built from plain rows is byte-identical to the per-row model path (LIBM-172).

[if] the plain-row index body differs by one byte from the model path [then] fail, [else stop].

The route validates the whole index once and serializes once, instead of building a
``TrackOut`` per row, dumping it and validating a listing row again. CORE's condition
for that speedup: the wire body stays byte-identical, field order and number
formatting included. ``tests.webui.track_index_golden_probe`` builds both bodies in a
fresh engine process over tests.webui.test_track_index's 2,300 mapped rows (with real
ANLZ and artwork files for some) and compares them.

Regression one-liners:
  - if the plain-row body and the model-path body differ in any byte then broken
  - if the comparison covers fewer rows than the fixture holds then broken
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.webui.listing_boot_probe import REPO_ROOT, share_root_under
from tests.webui.test_listing_boot_perf import _write_assets
from tests.webui.test_track_index import TRACKS, _seed

pytestmark = [pytest.mark.requirement("LIBM-172")]

ASSET_ROWS = 200


@pytest.fixture(scope="module")
def golden(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("track-index-golden")
    data_dir, home = _seed(root)
    share = share_root_under(home)
    for i in range(ASSET_ROWS):
        _write_assets(share, i)
    out = root / "golden.json"
    proc = subprocess.run(
        [sys.executable, "-m", "tests.webui.track_index_golden_probe", str(out)],
        cwd=REPO_ROOT,
        env={**os.environ, "MDT_DATA_DIR": str(data_dir), "HOME": str(home), "MDT_LIBRARY_MODE": "local"},
        capture_output=True, text=True, timeout=600, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(Path(out).read_text(encoding="utf-8"))


def test_plain_row_body_is_byte_identical_to_the_model_path(golden) -> None:
    """[if] the plain-row index body differs by one byte from the model path [then] fail, [else stop]."""
    print(f"new {golden['new']}, old {golden['old']}")
    assert golden["new"]["rows"] == golden["old"]["rows"] == TRACKS
    assert golden["first_difference"] is None, golden.get("context")
    assert golden["new"]["sha1"] == golden["old"]["sha1"]
