"""Walk the All Tracks listing the way the page does at boot, and time it.

Mini-PRD (statuses: ✔︎ done, ✔︎ ✅ done and run):
  ✔︎ ✅ R1 walk `GET /api/v1/tracks` page by page through an in-process engine
        over a data dir named by MDT_DATA_DIR (a COPY, never the live library)
        [if] MDT_DATA_DIR is unset or the adapter did not resolve it [then ⛔️]
        [if] any page answers non-200 [then ⛔️]
        [if] the walk returns zero rows [then ⛔️]
  ✔︎ ✅ R2 per page: handler wall ms, sqlite statements and connections on the
        request thread, payload bytes (identity and gzip), rows
        [if] a sqlite connection closed before its trace attached [then ⛔️]
  ✔︎ ✅ R3 optional cProfile of one page and a phase split (backend page read,
        row build, model build, serialization)
        [if] --profile names a page index the walk never reached [then ⛔️]

Usage (run from the repo root, in the worktree's own .venv):

    MDT_DATA_DIR=<copy> MDT_LIBRARY_MODE=local .venv/bin/python -m \
        scripts.perf.listing_boot_walk --limit 500 --walks 3 --out walk.json

The numbers are in-process (Starlette TestClient): no socket, no browser, no
competing boot requests. They bound the handler, not the page.
"""
from __future__ import annotations

import argparse
import cProfile
import gzip
import io
import json
import os
import pstats
import statistics
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from apps.adapters.rekordbox import config as rb_config
from apps.webui.server.app import create_app
from apps.webui.server.routes import tracks as tracks_route
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.webui.sql_trace import SqlTrace, statement_shapes, trace_sqlite

#: ``apps.webui.server.path_availability_refresh``'s worker thread name.
REFRESH_THREAD = "path-availability-refresh"
#: The other requests the page fires while the listing walks (measured on the
#: live preview, Thu 1 Oct 2026). ``--contend`` fires each ONCE, concurrently,
#: as the walk starts: a proxy for the boot burst, not a load test.
BOOT_BURST: tuple[tuple[str, str], ...] = (
    ("GET", "/api/v1/playlists?fast=true"),
    ("GET", "/api/v1/playlists"),
    ("GET", "/api/v1/reconcile/summary"),
    ("GET", "/api/v1/ingest/coverage?cached=true"),
    ("GET", "/api/v1/stems/cache/status"),
    ("GET", "/api/v1/usb/volumes"),
    ("GET", "/api/v1/feedback/comments"),
    ("GET", "/api/v1/cloudsync/status"),
    ("POST", "/api/v1/beatgrid-flags/scan"),
)


def _require_data_dir() -> Path:
    data_dir = Path(os.environ["MDT_DATA_DIR"])
    expected = data_dir / "state" / "state.db"
    if expected != rb_config.STATE_DB or not expected.is_file():
        raise SystemExit(f"MDT_DATA_DIR not honored: {rb_config.STATE_DB} vs {expected}")
    return data_dir


def _page_url(limit: int, cursor: str | None, extra: str) -> str:
    url = f"/api/v1/tracks?limit={limit}{extra}"
    return url if cursor is None else f"{url}&cursor={cursor}"


def _fire_boot_burst(client: TestClient, timings: dict[str, Any]) -> list[threading.Thread]:
    def fire(method: str, url: str) -> None:
        started = time.perf_counter()
        status = client.request(method, url).status_code
        timings[f"{method} {url}"] = [status, round((time.perf_counter() - started) * 1000, 1)]

    threads = [
        threading.Thread(target=fire, args=request, name=f"boot-burst-{index}")
        for index, request in enumerate(BOOT_BURST)
    ]
    for thread in threads:
        thread.start()
    return threads


def _walk(
    client: TestClient,
    trace: SqlTrace,
    limit: int,
    first_limit: int,
    extra: str,
    burst: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    burst_threads = _fire_boot_burst(client, burst) if burst is not None else []
    while True:
        page_limit = first_limit if not pages else limit
        trace.reset()
        started = time.perf_counter()
        response = client.get(
            _page_url(page_limit, cursor, extra), headers={"accept-encoding": "identity"},
        )
        wall_ms = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            raise SystemExit(f"page {len(pages)}: {response.status_code} {response.text[:300]}")
        if trace.untraced_outside(REFRESH_THREAD):
            raise SystemExit(f"page {len(pages)}: a connection closed before its trace attached")
        body = response.content
        payload = json.loads(body)
        # Under --contend the burst's own statements share the trace, so the
        # count is only attributable to the listing on an uncontended walk.
        statements = trace.statements_outside(REFRESH_THREAD)
        pages.append({
            "page": len(pages),
            "rows": len(payload["items"]),
            "wall_ms": round(wall_ms, 1),
            "statements": len(statements),
            "connections": trace.connections_outside(REFRESH_THREAD),
            "bytes": len(body),
            "gzip_bytes": len(gzip.compress(body, compresslevel=5)),
            "shapes": statement_shapes(statements).most_common(40),
        })
        cursor = payload.get("next_cursor")
        if cursor is None:
            for thread in burst_threads:
                thread.join()
            return pages


def _profile_page(
    app: Any, backend: SqliteBackend, limit: int, cursor: str | None, top: int,
) -> str:
    """cProfile the handler on THIS thread (the route runs in a worker thread
    under the client, where a profiler started here sees only the wait)."""
    request = Request({"type": "http", "app": app, "headers": [], "query_string": b""})
    call = lambda: tracks_route.list_tracks(  # noqa: E731
        request=request, q=None, bpm_min=None, bpm_max=None, key=None, rating_min=None,
        tag=None, available="all", show_deleted=False, cursor=cursor, limit=limit,
        backend=backend,
    )
    call()
    profiler = cProfile.Profile()
    started = time.perf_counter()
    profiler.enable()
    page = call()
    profiler.disable()
    handler_ms = (time.perf_counter() - started) * 1000
    # Round 0's handler returned the model and FastAPI rendered it; from round
    # 1 the handler returns rendered bytes, so there is nothing left to dump.
    started = time.perf_counter()
    body = page.body if isinstance(page, Response) else page.model_dump_json()
    dump_ms = (time.perf_counter() - started) * 1000
    out = io.StringIO()
    out.write(f"handler_ms={handler_ms:.1f} (under cProfile) model_dump_json_ms={dump_ms:.1f} "
              f"bytes={len(body)}\n")
    stats = pstats.Stats(profiler, stream=out)
    stats.sort_stats("cumulative").print_stats(top)
    stats.sort_stats("tottime").print_stats(top)
    stats.print_callers(r"posix\.(open|stat|lstat|fstat)|pathlib.*(resolve|is_file|stat)\b")
    return out.getvalue()


def _summary(walks: list[list[dict[str, Any]]]) -> dict[str, Any]:
    totals = [sum(p["wall_ms"] for p in walk) for walk in walks]
    last = walks[-1]
    return {
        "pages": len(last),
        "rows": sum(p["rows"] for p in last),
        "walk_ms": [round(t, 1) for t in totals],
        "walk_ms_median": round(statistics.median(totals), 1),
        "first_page_ms": [walk[0]["wall_ms"] for walk in walks],
        "first_page_rows": last[0]["rows"],
        "first_page_bytes": last[0]["bytes"],
        "page_ms_median": round(statistics.median(p["wall_ms"] for p in last), 1),
        "page_ms_first_full": last[1]["wall_ms"] if len(last) > 1 else None,
        "page_ms_last_full": last[-2]["wall_ms"] if len(last) > 2 else None,
        "statements_per_page": sorted({p["statements"] for p in last}),
        "connections_per_page": sorted({p["connections"] for p in last}),
        "bytes_total": sum(p["bytes"] for p in last),
        "gzip_bytes_total": sum(p["gzip_bytes"] for p in last),
        "bytes_per_row": round(sum(p["bytes"] for p in last) / sum(p["rows"] for p in last)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--first-limit", type=int, required=True)
    parser.add_argument("--walks", type=int, required=True)
    parser.add_argument("--extra", required=True, help="extra query string, e.g. '' or '&fields=table'")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--profile", type=int, help="page index to cProfile after the walks")
    parser.add_argument("--contend", action="store_true", help="fire BOOT_BURST as each walk starts")
    args = parser.parse_args()
    data_dir = _require_data_dir()
    state_db = data_dir / "state" / "state.db"
    # The trace is closed before profiling: its connection hook and cProfile
    # both claim the thread's profile slot.
    with ExitStack() as tracing:
        trace = tracing.enter_context(trace_sqlite())
        backend = SqliteBackend(state_db)
        app = create_app(
            backend=backend,
            bind_host="127.0.0.1",
            hostname="perf-host",
            state_db_path=str(state_db),
            mount_frontend=False,
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            bursts: list[dict[str, Any]] = [{} for _ in range(args.walks)]
            walks = [
                _walk(
                    client, trace, args.limit, args.first_limit, args.extra,
                    bursts[index] if args.contend else None,
                )
                for index in range(args.walks)
            ]
            tracing.close()
            if not sum(p["rows"] for p in walks[-1]):
                raise SystemExit("the walk returned zero rows: nothing was measured")
            profile_text = None
            if args.profile is not None:
                if args.profile >= len(walks[-1]):
                    raise SystemExit(f"--profile {args.profile}: the walk has {len(walks[-1])} pages")
                cursor = None
                for index in range(args.profile):
                    page = client.get(_page_url(
                        args.first_limit if index == 0 else args.limit, cursor, args.extra,
                    )).json()
                    cursor = page["next_cursor"]
                profile_text = _profile_page(
                    app, backend, args.first_limit if args.profile == 0 else args.limit, cursor, 60,
                )
    summary = _summary(walks)
    summary["contended"] = args.contend
    if args.contend:
        failed = {k: v for burst in bursts for k, v in burst.items() if v[0] >= 400}
        if failed:
            raise SystemExit(f"boot burst requests failed, the contention is not real: {failed}")
        summary["boot_burst_ms"] = bursts
    args.out.write_text(json.dumps({"summary": summary, "walks": walks}, indent=1), encoding="utf-8")
    if profile_text is not None:
        args.out.with_suffix(".profile.txt").write_text(profile_text, encoding="utf-8")
    json.dump(summary, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
