"""Throwaway real backend for frontend tests that must exercise the actual
midi_maps route rather than a fabricated fetch response (AGENTS.md "No mocks
and locked real fixtures").

Boots `create_app()` with an `InMemoryBackend` and a fresh temp data dir bound
to a real loopback socket, then prints ``LISTENING <port>`` once uvicorn is
serving. A Node test spawns this as a subprocess, reads that line to learn
the port, issues real HTTP requests against it, and kills the process when
done.

Usage: uv run --no-sync python -m scripts.testing.midi_maps_test_server [--port N] [--data-dir DIR]

``--data-dir`` lets a test seed the real files a route reads (for example
``state/ui-prefs.json``) instead of fabricating the route's response.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path


async def _serve(port: int, data_dir_arg: str | None) -> None:
    import uvicorn

    from apps.webui.server.app import create_app
    from apps.webui.server.backend import InMemoryBackend

    data_dir = (
        Path(data_dir_arg)
        if data_dir_arg is not None
        else Path(tempfile.mkdtemp(prefix="mdt-midi-maps-test-"))
    )
    if not data_dir.is_dir():
        raise SystemExit(f"--data-dir {data_dir} is not a directory")
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
        state_db_path=str(data_dir / "state" / "state.db"),
        client_error_log_dir=data_dir / "state" / "client-errors",
        client_event_log_dir=data_dir / "state" / "client-events",
    )
    app.state.data_dir = data_dir

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    while not server.started:
        if serve_task.done():
            serve_task.result()  # surface the startup failure, don't hang
        await asyncio.sleep(0.01)
    bound_port = server.servers[0].sockets[0].getsockname()[1]
    print(f"LISTENING {bound_port}", flush=True)
    await serve_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="0 = OS-assigned")
    parser.add_argument("--data-dir", default=None, help="existing dir; default a fresh temp dir")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_serve(args.port, args.data_dir))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
