"""The desktop payload carries the local Demucs worker script.

[if] the payload stages app source [then] it carries the vocal worker byte-identical, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.build_engine_payload import (
    VOCAL_WORKER_PAYLOAD_RELATIVE,
    VOCAL_WORKER_REPO_RELATIVE,
    stage_app_source,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.requirement("INSTALL-27")


def test_stage_app_source_copies_vocal_worker_into_payload(tmp_path: Path) -> None:
    build_dir = tmp_path / "frontend-build"
    build_dir.mkdir()
    (build_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    destination = tmp_path / "payload" / "app"
    stage_app_source(REPO_ROOT, destination, build_dir)

    staged = destination / VOCAL_WORKER_PAYLOAD_RELATIVE
    source = REPO_ROOT / VOCAL_WORKER_REPO_RELATIVE
    assert staged.is_file()
    assert staged.read_bytes() == source.read_bytes()
