"""A real updater archive for restart-to-update tests (INSTALL-39).

A gzip tar holding an ``Open DJ.app`` with a payload manifest, plus its
``.sig`` text, shaped like what ``just dmg`` writes to bundle/macos.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

BUILT_SHA = "eaf12c7a0a1b178168a1420fe495048959e4afc7"
SIGNATURE = "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZQ=="


def build_archive(
    tmp_path: Path,
    *,
    sha: str = BUILT_SHA,
    sign: bool = True,
    stamp: bool = True,
    name: str = "Open DJ.app.tar.gz",
    lineage: dict[str, Any] | None = None,
) -> Path:
    out = tmp_path / name
    manifest = {
        "identity": {
            "app_version": "1.0.0-alpha.1",
            "git_sha_full": sha,
            "built_at_utc": "2026-11-03T10:00:00Z",
            "release_channel": "local-unnotarized",
            **({} if lineage is None else {"lineage": lineage}),
        }
    }
    with tarfile.open(out, "w:gz") as archive:
        for directory in ("Open DJ.app", "Open DJ.app/Contents", "Open DJ.app/Contents/MacOS"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        binary = b"\x00binary"
        info = tarfile.TarInfo("Open DJ.app/Contents/MacOS/opendj-desktop")
        info.size = len(binary)
        archive.addfile(info, io.BytesIO(binary))
        if stamp:
            body = json.dumps(manifest).encode()
            info = tarfile.TarInfo("Open DJ.app/Contents/Resources/payload/manifest.json")
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    if sign:
        out.with_name(out.name + ".sig").write_text(SIGNATURE + "\n")
    return out
