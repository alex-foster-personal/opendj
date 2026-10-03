"""POST /ingest/upload TAG_READER_UNAVAILABLE 503, proved WITHOUT the tag reader.

[if] tinytag is absent [then] upload answers 503 TAG_READER_UNAVAILABLE and no .part stays, [else stop].

Regression one-liners:
  - if a reader-less upload returns 500 then broken
  - if a reader-less upload leaves a .part hold file then broken
  - if this test requires the reader installed to run then it proves nothing

Acceptance (issue #3070 / LIBMX-14; reader is tinytag since Thu 1 Oct 2026,
replacing the GPL mutagen extra):
  [if] tinytag is absent and a well-formed audio file is uploaded [then]
  a structured 503 names the missing reader, not a bare 500
  [if] tag-duration lookup raises ImportError inside _stage_one_upload [then]
  the route catches it before any bytes are staged, else stop
  [if] tinytag genuinely is installed [then] this route's existing behavior
  is unchanged, else stop
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("LIBMX-14")

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_upload_refuses_before_staging_when_tag_reader_genuinely_absent(
    tmp_path: Path,
) -> None:
    """503 TAG_READER_UNAVAILABLE under a genuinely blocked tinytag import."""
    ingest_root = tmp_path / "_ingest"
    src_mp3 = FIXTURE_ROOT / "src-128.mp3"
    probe = textwrap.dedent(
        f"""
            import sys
            sys.modules["tinytag"] = None  # force a genuine ImportError, not a flag flip

            from pathlib import Path

            from fastapi import FastAPI
            from fastapi.testclient import TestClient

            from apps.shared._tagreader import HAS_TAG_READER

            assert HAS_TAG_READER is False, "tinytag import was not actually blocked"

            from apps.webui.server.routes import ingest as ingest_mod
            from apps.webui.server.routes import ingest_upload as ingest_upload_mod

            ingest_root = Path({str(ingest_root)!r})
            ingest_mod.INGEST_INBOX = ingest_root

            app = FastAPI()
            app.include_router(ingest_upload_mod.router, prefix="/api/v1")

            audio_bytes = Path({str(src_mp3)!r}).read_bytes()
            batch = "adv5-dup-batch"
            dest_dir = ingest_root / batch

            with TestClient(app) as test_client:
                resp = test_client.post(
                    "/api/v1/ingest/upload",
                    files={{"files": ("dup.mp3", audio_bytes, "audio/mpeg")}},
                    data={{"batch": batch}},
                )

            assert resp.status_code == 503, resp.text
            detail = resp.json()["detail"]
            assert detail["code"] == "TAG_READER_UNAVAILABLE"
            assert "tinytag" in detail["message"]
            assert "uv sync" in detail["message"]

            hold = dest_dir / "dup.mp3.part"
            final = dest_dir / "dup.mp3"
            assert not hold.exists(), f"orphaned hold file: {{hold}}"
            assert not final.exists(), f"unexpected staged file: {{final}}"
            print("PROBE_OK")
        """
    )

    result = subprocess.run(
        [sys.executable, "-"],
        input=probe,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "PROBE_OK" in result.stdout, f"stdout={result.stdout}\nstderr={result.stderr}"
