"""Refresh signed payload metrics before the outer macOS app is resealed.

Supersedes: treating build-time payload digests as digests of signed bytes.
Input wheel, checkpoint, SPA and license digests retain their original meaning.
The caller must run this after the final inner signing step; this portable
metadata helper neither signs code nor claims that a signature is valid.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from scripts.build_engine_payload import (
    AUDIO_ENGINE_RELATIVE,
    MANIFEST_KIND,
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    sha256_file,
    sha256_tree,
    tree_bytes,
)
from scripts.payload_beatgrid import RUNNER_SITE_RELATIVE


def refresh_payload_manifest(payload_dir: Path) -> dict[str, Any]:
    """Recompute final byte metrics while preserving their build-time values."""
    payload_dir = payload_dir.resolve(strict=True)
    manifest_path = payload_dir / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("payload manifest must be a regular file")
    original_sha = sha256_file(manifest_path)
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or type(body.get("schema")) is not int:
        raise ValueError("payload manifest must declare its integer schema")
    if body["schema"] != MANIFEST_SCHEMA or body.get("kind") != MANIFEST_KIND:
        raise ValueError("unsupported payload manifest schema or kind")
    source_sha = body["identity"]["git_sha_full"]
    if not isinstance(source_sha, str) or len(source_sha) != 40 or any(
        character not in "0123456789abcdef" for character in source_sha
    ):
        raise ValueError("payload identity must declare its full source SHA")
    if "hash_provenance" in body:
        raise ValueError("payload metrics were already refreshed; preserving original provenance")
    if body["audio_engine"]["path"] != AUDIO_ENGINE_RELATIVE:
        raise ValueError("unexpected audio engine path")
    if body["beatgrid_runner"]["site"] != str(RUNNER_SITE_RELATIVE):
        raise ValueError("unexpected beatgrid runner site")
    for relative in ("runtime", "pylib", "app", str(RUNNER_SITE_RELATIVE)):
        if not (payload_dir / relative).is_dir():
            raise ValueError(f"payload directory is missing: {relative}")
    native = payload_dir / AUDIO_ENGINE_RELATIVE
    if native.is_symlink() or not native.is_file():
        raise ValueError("audio engine must be a regular file")
    before = {
        "sha256": copy.deepcopy(body["sha256"]),
        "bytes": copy.deepcopy(body["bytes"]),
        "audio_engine_sha256": body["audio_engine"]["sha256"],
        "beatgrid_runner_site_bytes": body["beatgrid_runner"]["bytes"]["site"],
    }
    body["sha256"] = {
        name: sha256_tree(payload_dir / name) for name in ("runtime", "pylib", "app")
    }
    body["bytes"] = {
        name: tree_bytes(payload_dir / name) for name in ("runtime", "pylib", "app")
    }
    # The builder writes the manifest last: its total excludes that file.
    # Retain this scope after refresh, avoiding a self-referential byte count.
    body["bytes"]["total"] = tree_bytes(payload_dir) - manifest_path.stat().st_size
    body["audio_engine"]["sha256"] = sha256_file(native)
    body["beatgrid_runner"]["bytes"]["site"] = tree_bytes(payload_dir / RUNNER_SITE_RELATIVE)
    body["hash_provenance"] = {
        "schema": 1,
        "phase": "after-final-inner-signing-before-outer-seal",
        "manifest_sha256_before_refresh": original_sha,
        "build_before_inner_signing": before,
        "bytes_total_scope": "payload-files-excluding-manifest.json",
    }
    encoded = json.dumps(body, indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=payload_dir,
            prefix=".manifest-refresh-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # This is public package metadata: retain its existing read permissions.
        temporary.chmod(stat.S_IMODE(manifest_path.stat().st_mode))
        os.replace(temporary, manifest_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, type=Path)
    args = parser.parse_args()
    try:
        body = refresh_payload_manifest(args.payload)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"[ERROR] signed payload metrics: {exc}\n")
    print(
        "[OK] final signed payload metrics refreshed; "
        f"source={body['identity']['git_sha_full']} native={body['audio_engine']['sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
