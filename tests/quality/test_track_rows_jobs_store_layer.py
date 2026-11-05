"""track_rows must not import engine_core.jobs.store (issue #5806)."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "apps" / "webui" / "server" / "rb_vendor_pkg" / "track_rows.py"


def test_track_rows_does_not_import_engine_core_jobs_store() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    assert "apps.engine_core.jobs.store" not in text
    assert "JobStoreListLike" in text
