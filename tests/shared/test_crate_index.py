"""Real-file coverage for the owner/replica crate ledger."""

from __future__ import annotations

import json
from pathlib import Path

from apps.shared.crate_index import (
    audit_manifest,
    ledger_digest,
    resolve_crate_audio,
)

SID = "c" * 40


def _manifest(crate: Path, audio: Path) -> dict[str, object]:
    stat = audio.stat()
    files: list[dict[str, object]] = [
        {
            "bytes": stat.st_size,
            "dest": str(audio),
            "dest_root": str(crate / "users" / "dev"),
            "kind": "audio",
            "mtime_s": int(stat.st_mtime),
            "relative": "Music/Tina.mp3",
            "source": "/Users/dev/Music/Tina.mp3",
            "source_root": "/Users/dev",
            "stable_ids": [SID],
        }
    ]
    return {
        "bytes": stat.st_size,
        "count": 1,
        "crate_root": str(crate),
        "files": files,
        "ledger_digest": ledger_digest(files),
        "scope": "playlist:Base",
        "source_host": "owner",
        "track_files": {SID: str(audio)},
    }


def test_remote_index_resolves_by_stable_id_but_local_mode_never_uses_it(
    tmp_path: Path,
) -> None:
    crate = tmp_path / "crate"
    audio = crate / "users" / "dev" / "Music" / "Tina.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ID3-real-audio")
    manifest = _manifest(crate, audio)
    manifest_path = crate / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert (
        resolve_crate_audio(
            SID,
            environ={"MDT_LIBRARY_MODE": "local"},
            manifest_path=manifest_path,
        )
        is None
    )
    assert (
        resolve_crate_audio(
            SID,
            environ={
                "MDT_LIBRARY_MODE": "remote",
                "MDT_CRATE_ROOT": str(crate),
            },
            manifest_path=manifest_path,
        )
        == audio
    )


def test_replica_audit_detects_real_missing_and_changed_files(tmp_path: Path) -> None:
    crate = tmp_path / "crate"
    audio = crate / "users" / "dev" / "Music" / "Tina.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"ID3-real-audio")
    manifest = _manifest(crate, audio)

    matched = audit_manifest(manifest, crate_root=crate)
    assert matched.ok is True
    assert matched.expected_digest == matched.actual_digest

    audio.write_bytes(b"changed-size")
    changed = audit_manifest(manifest, crate_root=crate)
    assert changed.ok is False
    assert changed.size_mismatches == (str(audio),)

    audio.unlink()
    missing = audit_manifest(manifest, crate_root=crate)
    assert missing.ok is False
    assert missing.missing == (str(audio),)
