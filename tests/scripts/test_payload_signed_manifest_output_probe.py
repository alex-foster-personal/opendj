"""The post-sign manifest refresh re-hashes the output-probe helper (ELECTRON-6272).

[if] signing rewrites bin/odj-output-probe after the build hashed it [then] the refresh re-hashes it, keeping the build digest, [else stop].
[if] the payload has no helper (off darwin) [then] the refresh leaves the skip block alone, [else stop].

Real files and the real refresh: the "signing" here is a real rewrite of the
helper's bytes, which is what codesign does to a Mach-O.

-Grok
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.build_engine_payload import (
    AUDIO_ENGINE_RELATIVE,
    MANIFEST_KIND,
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    sha256_file,
)
from scripts.payload_beatgrid import RUNNER_SITE_RELATIVE
from scripts.payload_output_probe import OUTPUT_PROBE_RELATIVE
from scripts.payload_signed_manifest import refresh_payload_manifest

pytestmark = pytest.mark.requirement("ELECTRON-6272")


def _payload(root: Path, output_probe: dict[str, Any]) -> Path:
    for name in ("runtime", "pylib", "app", str(RUNNER_SITE_RELATIVE)):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "f.txt").write_text(name, encoding="utf-8")
    native = root / AUDIO_ENGINE_RELATIVE
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_bytes(b"engine")
    body = {
        "schema": MANIFEST_SCHEMA,
        "kind": MANIFEST_KIND,
        "identity": {"git_sha_full": "a" * 40},
        "sha256": {},
        "bytes": {},
        "audio_engine": {"path": AUDIO_ENGINE_RELATIVE, "sha256": sha256_file(native)},
        "beatgrid_runner": {"site": str(RUNNER_SITE_RELATIVE), "bytes": {"site": 0}},
        "output_probe": output_probe,
    }
    (root / MANIFEST_NAME).write_text(json.dumps(body), encoding="utf-8")
    return root


def test_a_signed_helper_is_re_hashed_and_its_build_digest_kept(tmp_path: Path) -> None:
    helper = tmp_path / OUTPUT_PROBE_RELATIVE
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"unsigned helper")
    built = sha256_file(helper)
    payload = _payload(tmp_path, {"staged": True, "path": OUTPUT_PROBE_RELATIVE, "sha256": built})
    helper.write_bytes(b"unsigned helper + code signature")  # what codesign does to the file
    body = refresh_payload_manifest(payload)
    assert body["output_probe"]["sha256"] == sha256_file(helper) != built
    assert body["hash_provenance"]["build_before_inner_signing"]["output_probe_sha256"] == built
    on_disk = json.loads((payload / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert on_disk["output_probe"]["sha256"] == sha256_file(helper)


def test_a_payload_without_the_helper_keeps_its_skip_block(tmp_path: Path) -> None:
    skipped = {"staged": False, "path": OUTPUT_PROBE_RELATIVE, "reason": "macOS-only"}
    body = refresh_payload_manifest(_payload(tmp_path, dict(skipped)))
    assert body["output_probe"] == skipped
    assert "output_probe_sha256" not in body["hash_provenance"]["build_before_inner_signing"]


def test_a_symlinked_helper_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.write_bytes(b"x")
    helper = tmp_path / OUTPUT_PROBE_RELATIVE
    helper.parent.mkdir(parents=True)
    helper.symlink_to(real)
    payload = _payload(tmp_path, {"staged": True, "path": OUTPUT_PROBE_RELATIVE, "sha256": "0" * 64})
    with pytest.raises(ValueError, match="output probe must be a regular file"):
        refresh_payload_manifest(payload)
