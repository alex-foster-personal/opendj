"""The real daemon app on a loopback port, for node tests of the API clients.

Backs ``reanalyze-real-daemon.test.mjs`` and ``play-analytics-real-daemon.test.mjs``.
Both replaced tests that answered the client with a hand-written ``fetch``
response (PR #5054 review): one fabricated the 503 text the analytics client
matches on, the other replaced the Re-analyze dependencies with recorders, so
neither could see a failure between the client and the daemon.

This serves the PRODUCTION app (``create_app``), with every route, middleware
and exception handler the daemon has, over a scratch data dir:

* ``MDT_DATA_DIR`` points at the scratch dir BEFORE any ``apps`` import, so the
  play-analytics router's event store (``<data>/sets/sets.db``) is genuinely
  missing, the state a library that never recorded a set is in.
* The library holds one live track, made by the production folder import
  (``importer.run_folder_import``) from a digest-checked copy of the tracked
  16 s audio fixture (``tests/webui/drain_media.py``), so Re-analyze's enqueue
  admits real media and the daemon's real backfill drain analyzes it. No row
  is written by hand (Codex P1 on PR #5054).
* Whether this host can run the Re-analyze producer (own beatgrid needs the
  verified Beat This! checkpoint) is measured by the producer's own checkpoint
  resolution and announced, so the test can demand a successful analysis
  wherever the capability exists and a named UNAVAILABLE where it does not.

``POST /test/shutdown`` is the only route added, for ``stop-fixture-server.mjs``.

Usage: ``uv run --no-sync python real_daemon_server.py``, then read the
``SCRATCH <dir>``, ``TRACK <stable_id>``, ``BEATGRID <ok|unavailable> <detail>``
and ``READY <port>`` stdout lines.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import sys
import tempfile
from pathlib import Path

#: Outside the tracked tree: opening a state db regenerates a sibling AGENTS.md.
_SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="real-daemon-server-"))
os.environ["MDT_DATA_DIR"] = str(_SCRATCH_DIR)

import uvicorn  # noqa: E402 -- after MDT_DATA_DIR, which apps.* reads at import

from apps.analysis.pcm_fingerprint import FingerprintUnavailable, require_resampler  # noqa: E402
from apps.analysis_beatgrid import weights  # noqa: E402
from apps.webui.server.app import create_app  # noqa: E402
from tests.webui import drain_media  # noqa: E402

_STATE_DB: Path = drain_media.state_db(_SCRATCH_DIR)
#: The file name of the one track Re-analyze targets; its stable_id is the import's.
REANALYZE_NAME = "real-daemon-reanalyze-track"


def _import_library() -> str:
    """The Re-analyze target's stable_id, as the production folder import assigned it."""
    return drain_media.import_tracks(_SCRATCH_DIR, [REANALYZE_NAME])[REANALYZE_NAME]


def _beatgrid_capability() -> str:
    """``ok <sha256>`` or ``unavailable <reason>``: the two host capabilities the own
    beatgrid producer checks before it analyzes anything (``OwnBeatgridBackfillBackend``),
    measured by the same calls."""
    try:
        _path, digest = weights.resolve_checkpoint()
        require_resampler()
    except (weights.WeightsError, FingerprintUnavailable) as exc:
        return f"unavailable {type(exc).__name__}: {exc}"
    return f"ok {digest}"


def main() -> int:
    # Preferred stop is POST /test/shutdown (portable). The SIGTERM fallback
    # still removes the scratch dir on POSIX.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        _serve()
    finally:
        shutil.rmtree(_SCRATCH_DIR, ignore_errors=True)
    return 0


def _serve() -> None:
    stable_id = _import_library()
    capability = _beatgrid_capability()
    app = create_app(state_db_path=str(_STATE_DB), mount_frontend=False)
    app.state.analysis_db_path = _STATE_DB

    @app.post("/test/shutdown")
    def _shutdown() -> dict[str, bool]:
        app.state.server.should_exit = True
        return {"ok": True}

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    # listen() BEFORE the READY line, so a reader connecting at once queues in
    # the backlog instead of racing uvicorn's own startup to ECONNREFUSED.
    sock.listen()
    port = sock.getsockname()[1]
    print(f"SCRATCH {_SCRATCH_DIR}", flush=True)
    print(f"TRACK {stable_id}", flush=True)
    print(f"BEATGRID {' '.join(capability.split())}", flush=True)
    print(f"READY {port}", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, fd=sock.fileno(), log_level="warning"))
    app.state.server = server
    server.run()


if __name__ == "__main__":
    raise SystemExit(main())
