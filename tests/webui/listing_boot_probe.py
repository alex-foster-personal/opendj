"""Measure listing pages over a rekordbox-mapped fixture, in a fresh process (LIBM-137).

``tests.webui.listing_probe`` counts sqlite work for rows with NO rekordbox
mapping. The All Tracks boot cost found on Thu 1 Oct 2026 was somewhere else:
every MAPPED row re-resolved its analysis and artwork paths through the
share-root containment walk on every request, about 17 file opens a row. This
probe counts that, on the real functions:

* file opens, from CPython's own ``open`` audit event (``os.open``,
  ``open``), attributed to the thread that made them;
* sqlite statements and connections, from ``tests.webui.sql_trace``.

Nothing is replaced. The engine runs in a child interpreter whose environment
carries ``MDT_DATA_DIR`` (state.db, master.plain.db) and ``HOME`` (the
rekordbox share root hangs off it), which is how the packaged engine finds
them. The child refuses to run unless both resolved to the fixture.

The spec is a list of steps run in order against one engine, so a test can
change the filesystem between two requests and see what the next one says:

    {"op": "get", "name": "warm", "url": "/api/v1/tracks?limit=500"}
    {"op": "write", "path": "...", "hex": "...", "mtime_ns": 1}
    {"op": "unlink", "path": "..."}
    {"op": "swap_dir_for_symlink", "path": "...", "target": "..."}

    python -m tests.webui.listing_boot_probe <spec.json>
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared import platform_paths
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.webui.sql_trace import SqlTrace, statement_shapes, trace_sqlite

REPO_ROOT = Path(__file__).resolve().parents[2]
#: ``apps.webui.server.path_availability_refresh``'s worker thread name.
REFRESH_THREAD = "path-availability-refresh"
#: Starlette runs a sync route in an anyio worker thread with this name prefix.
REQUEST_THREAD_PREFIX = "AnyIO worker thread"


#-----
# Parent side.


def share_root_under(home: Path) -> Path:
    """Where a child started with ``HOME=home`` looks for the rekordbox share."""
    return home / platform_paths.rekordbox_app_dir().relative_to(platform_paths.HOME) / "share"


def run_boot_probe(data_dir: Path, home: Path, steps: list[dict[str, Any]], scratch: Path) -> dict[str, Any]:
    """Run ``steps`` against one engine in a child process and return its JSON."""
    spec_path, out_path = scratch / "boot-probe-spec.json", scratch / "boot-probe-out.json"
    spec = {"steps": steps, "out": str(out_path), "share_root": str(share_root_under(home))}
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "tests.webui.listing_boot_probe", str(spec_path)],
        cwd=REPO_ROOT,
        env={**os.environ, "MDT_DATA_DIR": str(data_dir), "HOME": str(home), "MDT_LIBRARY_MODE": "local"},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"listing boot probe exited {proc.returncode}; its stderr tail:\n{proc.stderr[-4000:]}"
        )
    result: dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8"))
    return result


#-----
# Child side.

_OPENS: Counter[str] = Counter()
_COUNTING = threading.Event()


def _count_opens(event: str, _args: tuple[Any, ...]) -> None:
    if event == "open" and _COUNTING.is_set():
        _OPENS[threading.current_thread().name] += 1


def _require_the_env_contract(spec: dict[str, Any]) -> Path:
    data_dir = Path(os.environ["MDT_DATA_DIR"])
    expected = (data_dir / "state" / "state.db", data_dir / "master.plain.db", Path(spec["share_root"]))
    resolved = (rb_config.STATE_DB, rb_config.MASTER_PLAIN_DB, platform_paths.SHARE_ROOT)
    if resolved != expected:
        raise SystemExit(f"fixture env was not honored: resolved {resolved}, expected {expected}")
    return data_dir


def _get(client: TestClient, trace: SqlTrace, url: str) -> dict[str, Any]:
    trace.reset()
    _OPENS.clear()
    _COUNTING.set()
    started = time.perf_counter()
    response = client.get(url, headers={"accept-encoding": "identity"})
    wall_ms = (time.perf_counter() - started) * 1000
    _COUNTING.clear()
    if response.status_code != 200:
        raise SystemExit(f"{url}: answered {response.status_code}: {response.text[:500]}")
    if trace.untraced_outside(REFRESH_THREAD):
        raise SystemExit(f"{url}: a connection closed before its trace attached")
    statements = trace.statements_outside(REFRESH_THREAD)
    return {
        "body": response.json(),
        "bytes": len(response.content),
        "wall_ms": wall_ms,
        "content_type": response.headers["content-type"],
        "request_opens": sum(
            count for name, count in _OPENS.items() if name.startswith(REQUEST_THREAD_PREFIX)
        ),
        "opens_by_thread": dict(_OPENS),
        "connections": trace.connections_outside(REFRESH_THREAD),
        "statements": len(statements),
        "shapes": sorted(statement_shapes(statements).items()),
    }


def _mutate(step: dict[str, Any]) -> None:
    path = Path(step["path"])
    if step["op"] == "write":
        path.write_bytes(bytes.fromhex(step["hex"]))
        os.utime(path, ns=(step["mtime_ns"], step["mtime_ns"]))
    elif step["op"] == "unlink":
        path.unlink()
    elif step["op"] == "swap_dir_for_symlink":
        path.rename(path.with_name(path.name + ".moved-aside"))
        path.symlink_to(step["target"], target_is_directory=True)
    else:
        raise SystemExit(f"unknown probe step: {step['op']!r}")


def main(spec_path: str) -> None:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    data_dir = _require_the_env_contract(spec)
    state_db = data_dir / "state" / "state.db"
    sys.addaudithook(_count_opens)
    results: dict[str, Any] = {}
    with trace_sqlite() as trace:
        app = create_app(
            backend=SqliteBackend(state_db),
            bind_host="127.0.0.1",
            hostname="test-host",
            state_db_path=str(state_db),
            mount_frontend=False,
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            for step in spec["steps"]:
                if step["op"] == "get":
                    results[step["name"]] = _get(client, trace, step["url"])
                else:
                    _mutate(step)
    Path(spec["out"]).write_text(json.dumps(results), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
