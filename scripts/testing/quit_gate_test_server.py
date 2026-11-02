"""Throwaway real engine for quit-gate acceptance (issue #6262).

Boots ``apps.engine_core.app.create_app`` with a fresh temp data dir, the live
``/api/v1/events`` WebSocket hub, and ``POST /api/v1/lifecycle/quit``. Node
unit tests spawn this, read ``LISTENING <port>``, exercise the production
events bus and lifecycle route, then kill the process.

Usage: uv run --no-sync python -m scripts.testing.quit_gate_test_server [--port N]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path


async def _serve(port: int) -> None:
    # Set before the apps imports: shared paths freeze MDT_DATA_DIR at import.
    data_dir = Path(tempfile.mkdtemp(prefix="mdt-quit-gate-test-"))
    os.environ["MDT_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("MDT_LIBRARY_MODE", "local")
    os.environ.setdefault("MUSIC_DJ_AUTO_ANALYZE", "off")

    import uvicorn

    from apps.engine_core.app import create_app
    from apps.engine_core.config import EngineConfig

    cfg = EngineConfig(data_dir=data_dir, host="127.0.0.1", port=0)
    app = create_app(cfg)

    config = uvicorn.Config(app, host=cfg.host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    while not server.started:
        if serve_task.done():
            serve_task.result()
        await asyncio.sleep(0.01)
    bound_port = server.servers[0].sockets[0].getsockname()[1]
    print(f"LISTENING {bound_port}", flush=True)
    await serve_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="0 = OS-assigned")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_serve(args.port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
