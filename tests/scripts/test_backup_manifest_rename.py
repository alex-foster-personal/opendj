"""Music-backup manifest path and store host listing errors (#5639 stage 2).

Regression one-liners:
  - if the writer and the reader disagree on the manifest path then broken
  - if the store-listing error label does not follow MDT_B2STORE_SSH_HOST then broken
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.b2store import B2STORE_SSH_HOST_ENV
from scripts.backup_manifest import MUSIC_MANIFEST_REL

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_manifest_is_named_for_its_role() -> None:
    """[if] the manifest path still names a host [then] fail, [else stop]."""
    assert Path("(private path)") == MUSIC_MANIFEST_REL


def _run_fresh(code: str, store_host: str | None, *, expect_ok: bool = True) -> str:
    env = {k: v for k, v in os.environ.items() if k != B2STORE_SSH_HOST_ENV}
    if store_host is not None:
        env[B2STORE_SSH_HOST_ENV] = store_host
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if expect_ok:
        assert proc.returncode == 0, proc.stderr[-3000:]
    else:
        assert proc.returncode != 0
    return (proc.stdout or proc.stderr).strip()


def test_writer_and_reader_share_the_manifest_path() -> None:
    """[if] backup_music_to_bifrost2 and backup_evicted_to_usb disagree on the path [then] fail, [else stop]."""
    code = (
        "import json\n"
        "from scripts import backup_evicted_to_usb as r, backup_music_to_bifrost2 as w\n"
        "print(json.dumps([str(w.MANIFEST_PATH), str(r.MANIFEST)]))\n"
    )
    writer, reader = json.loads(_run_fresh(code, None))
    expected = str(REPO_ROOT / MUSIC_MANIFEST_REL)
    assert writer == reader == expected


@pytest.mark.parametrize("store_host", [None, "", "  "], ids=["unset", "blank", "spaces"])
def test_listing_error_names_the_variable_when_host_unset(store_host: str | None) -> None:
    """[if] parse_sizes without a configured host does not name MDT_B2STORE_SSH_HOST [then] fail, [else stop]."""
    code = (
        "import sys\n"
        "from scripts.b2shell import parse_sizes\n"
        "try:\n"
        "    parse_sizes('not a size row\\n')\n"
        "except ValueError as exc:\n"
        "    print(exc)\n"
        "    sys.exit(1)\n"
    )
    out = _run_fresh(code, store_host, expect_ok=False)
    assert B2STORE_SSH_HOST_ENV in out
    assert "unparseable find output" not in out


def test_listing_error_names_the_configured_store_when_set() -> None:
    """[if] b2shell's parse error names a host other than the configured store [then] fail, [else stop]."""
    code = (
        "from scripts.b2shell import parse_sizes\n"
        "try:\n"
        "    parse_sizes('not a size row\\n')\n"
        "except ValueError as exc:\n"
        "    print(exc)\n"
    )
    host = "asset-store-test-host"
    assert _run_fresh(code, host).splitlines()[-1] == (
        f"unparseable find output from {host}: 'not a size row'"
    )
