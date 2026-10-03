"""Measure a data dir's sqlite work in a fresh process (LIBM-130).

The adapter reads its database paths once, at import, from ``MDT_DATA_DIR``
(``apps.adapters.rekordbox.config``). Pointing it at a fixture from inside a
test process would mean rebinding those module attributes, which the test
contract forbids. So the tests seed a data dir, then run this module in a
child interpreter whose environment carries ``MDT_DATA_DIR`` - the same
contract the packaged engine and ``run_daemon.py`` use - and read back JSON.

Module-level imports are safe on both sides: the child's environment carries
``MDT_DATA_DIR`` before its first import, and the parent only calls
:func:`run_probe`.

The child refuses to measure unless the adapter really resolved the fixture
paths (a positive control on the contract itself), and it exits non-zero on
any failed request, so a broken probe can never read as a clean count.

    python -m tests.webui.listing_probe <spec.json>
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox.paths import local_artwork_available
from apps.shared._tagreader import HAS_TAG_READER
from apps.webui.server.app import create_app
from apps.webui.server.rb_vendor_pkg.track_rows import bulk_rb_meta
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.webui.sql_trace import SqlTrace, trace_sqlite

REPO_ROOT = Path(__file__).resolve().parents[2]
#: ``apps.webui.server.path_availability_refresh``'s worker thread name.
REFRESH_THREAD = "path-availability-refresh"


#-----
# Parent side: run the probe and hand back its result.


def run_probe(data_dir: Path, spec: dict[str, Any], scratch: Path) -> dict[str, Any]:
    """Run one probe against ``data_dir`` in a child process and return its JSON."""
    spec_path, out_path = scratch / "probe-spec.json", scratch / "probe-out.json"
    spec_path.write_text(json.dumps({**spec, "out": str(out_path)}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "tests.webui.listing_probe", str(spec_path)],
        cwd=REPO_ROOT,
        env={**os.environ, "MDT_DATA_DIR": str(data_dir)},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"listing probe exited {proc.returncode}; its stderr tail:\n{proc.stderr[-4000:]}"
        )
    result: dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8"))
    return result


#-----
# Child side: runs under MDT_DATA_DIR, in a fresh interpreter.


def _require_the_env_contract() -> Path:
    data_dir = Path(os.environ["MDT_DATA_DIR"])
    expected = (data_dir / "state" / "state.db", data_dir / "master.plain.db")
    resolved = (rb_config.STATE_DB, rb_config.MASTER_PLAIN_DB)
    if resolved != expected:
        raise SystemExit(f"MDT_DATA_DIR was not honored: resolved {resolved}, expected {expected}")
    return data_dir


def _measure(client: TestClient, trace: SqlTrace, url: str) -> dict[str, Any]:
    """Connections and statements of one warm request, request threads only.

    The first call warms per-process caches (machine id, the availability
    L1) that are not the defect. The background availability refresher is
    excluded: it runs on its own schedule, not per listed row.
    """
    warm = client.get(url)
    if warm.status_code != 200:
        raise SystemExit(f"{url}: warm-up answered {warm.status_code}: {warm.text[:500]}")
    trace.reset()
    response = client.get(url)
    if response.status_code != 200:
        raise SystemExit(f"{url}: answered {response.status_code}: {response.text[:500]}")
    if trace.untraced_outside(REFRESH_THREAD):
        raise SystemExit(f"{url}: a connection closed before its trace attached")
    return {
        "connections": trace.connections_outside(REFRESH_THREAD),
        "statements": trace.statements_outside(REFRESH_THREAD),
    }


def _probe_listing(spec: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    state_db = data_dir / "state" / "state.db"
    with trace_sqlite() as trace:
        app = create_app(
            backend=SqliteBackend(state_db),
            bind_host="127.0.0.1",
            hostname="test-host",
            state_db_path=str(state_db),
            mount_frontend=False,
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            measured = {url: _measure(client, trace, url) for url in spec["measure"]}
            fetched = {name: client.get(url).json() for name, url in spec["fetch"].items()}
    return {
        "measured": measured,
        "fetched": fetched,
        "oracle": {sid: local_artwork_available(sid) for sid in spec["oracle_ids"]},
        "has_tag_reader": HAS_TAG_READER,
    }


def _probe_rb_meta(spec: dict[str, Any], _data_dir: Path) -> dict[str, Any]:
    with trace_sqlite() as trace:
        meta = bulk_rb_meta(spec["ids"])
    return {"meta_ids": sorted(meta), "statements": [sql for _thread, sql in trace.statements]}


def main(spec_path: str) -> None:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    data_dir = _require_the_env_contract()
    probes = {"listing": _probe_listing, "rb_meta": _probe_rb_meta}
    result = probes[spec["mode"]](spec, data_dir)
    Path(spec["out"]).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
