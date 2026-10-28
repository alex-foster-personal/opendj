"""Capture the GET /api/v1/tracks/index body the Cmd K v2 tests read (CMDK-04, CMDK-05).

Codex P1 on PR #6089 (thread PRRT_kwDOSEvNd86rCuos): the Cmd K tests built their
rows from an invented factory. This writes a real index body instead: a disposable
data dir is hydrated through the production ``StateWriter`` (the calls an import
makes), then a CHILD interpreter boots the engine composition root from it with
``MDT_DATA_DIR`` set before any import (the path test_unmapped_streaming_provider
uses), and the index is read through that app. No monkeypatching.

    uv run --no-sync python -m tests.webui.capture_cmdk_index <out.json>

The output is a capture: commit it with its manifest and never rewrite it to make
a test pass. Re-capture only for a reviewed change to the index wire shape.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from tests.webui.test_unmapped_streaming_provider import _ENGINE_READ, REAL_AUDIO, REAL_AUDIO_SHA256, REPO_ROOT

#: (stable id, title, artist, album, file). ``None`` file means the present real mp3;
#: a str is used as is (a streaming URI, or a local path that does not exist).
SPECS = [
    ("1" * 40, "Lanternfall", "Ostra Vell", "Kestrelmoor Sessions", None),
    ("2" * 40, "Lanternfall (Dub Remix)", "Ostra Vell", None, None),
    ("3" * 40, "Glass Tide", "Mira Quell", "Kestrelmoor Sessions", None),
    ("4" * 40, "Lanternfall Static", "Ostra Vell", None, "/missing/lanternfall-static.mp3"),
    ("5" * 40, "Lanternfall Stream", "Tidal Act", None, "tidal:track:6089"),
]
PLAYLIST_ID = "pl-cmdk-v2"
#: Comments written through the production field path: a word that appears in no
#: other searched field, so a comment-only match is testable (Codex P2 on #6089).
COMMENTS = {"3" * 40: "warmup opener for saltmarsh nights"}


def hydrate(data_dir: Path) -> None:
    audio = data_dir / "audio" / "local.mp3"
    audio.parent.mkdir(parents=True)
    digest = hashlib.sha256(REAL_AUDIO.read_bytes()).hexdigest()
    assert digest == REAL_AUDIO_SHA256, f"{REAL_AUDIO} changed: {digest}"
    conn = state_db.open_rw(data_dir / "state" / "state.db")
    try:
        writer = StateWriter(conn, actor="capture")
        for sid, title, artist, album, file_path in SPECS:
            path = file_path
            if path is None:
                path = str(audio.with_name(f"{sid[0]}.mp3"))
                shutil.copy2(REAL_AUDIO, path)
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=title,
                artists=[artist],
                album=album,
                isrc=None,
                duration_ms=180_000,
                file_path=path,
            )
        for sid, text in COMMENTS.items():
            writer.set_field(sid, "comments", text, source="rekordbox", modified_at="2026-10-13T00:00:00Z")
        writer.insert_playlist(playlist_id=PLAYLIST_ID, name="Cmd K", vendor="djay", vendor_pl_id="djay-cmdk")
        writer.set_playlist_memberships(PLAYLIST_ID, [s[0] for s in SPECS])
        writer.close()
    finally:
        conn.close()


def main(out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        hydrate(data_dir)
        env = {**os.environ, "MDT_DATA_DIR": str(data_dir), "MDT_LIBRARY_MODE": "local", "PYTHONPATH": str(REPO_ROOT)}
        listings = {"index": ("/api/v1/tracks/index", "items"), "revision": ("/api/v1/tracks/index", "revision")}
        proc = subprocess.run(
            [sys.executable, "-c", _ENGINE_READ, str(data_dir), json.dumps(listings)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["index"]["status"] == 200, result
    items = result["index"]["rows"]
    for item in items:  # temp paths differ per run; the tests never read them
        if not str(item.get("file_path", "")).startswith(("tidal:", "/missing/")):
            item["file_path"] = "<data_dir>/audio/" + Path(item["file_path"]).name
    body = {"revision": result["revision"]["rows"], "items": items}
    out.write_text(
        "\n".join(["{", f'"revision": {json.dumps(body["revision"])},', '"items": ['])
        + "\n"
        + ",\n".join(json.dumps(i, sort_keys=True) for i in items)
        + "\n]}\n",
        encoding="utf-8",
        newline="\n",  # LF on every platform, so the bytes match the manifest's sha256
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
