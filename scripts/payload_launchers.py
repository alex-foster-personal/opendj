"""Shared launcher file writer for engine payload staging scripts."""

from __future__ import annotations

from pathlib import Path


def write_payload_launcher(
    payload_dir: Path,
    relative_path: str | Path,
    template: str,
) -> Path:
    """Write a UTF-8 launcher shell script under ``payload_dir`` and make it executable."""
    launcher = payload_dir / relative_path
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(template, encoding="utf-8")
    launcher.chmod(0o755)
    return launcher
