"""A loopback hub that answers the production SYNC_WIRE_VERSION 409.

The real hub gate compares the offered wire to this process's WIRE_VERSION,
so a same-process uvicorn hub cannot refuse a same-process spoke. This app
is the captured hello refusal body (same shape as
``apps.sync_hub.service._require_same_wire``) served over a real socket so
``HttpTransport`` and the feedback/ops routes run the production path.
"""
from __future__ import annotations

from collections.abc import Iterator

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from apps.shared.state import schema as state_schema
from apps.sync_hub import wire_version
from tests.cloudsync.conftest import free_port
from tests.waits import start_uvicorn_in_thread


def wire_mismatch_app() -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/sync/hello")
    async def hello(request: Request) -> None:
        body = await request.json()
        offered = body.get("wire_version")
        hub_wire = wire_version.WIRE_VERSION - 1
        raise HTTPException(
            status_code=409,
            detail={
                "code": wire_version.CODE_WIRE,
                "message": (
                    f"peer speaks sync wire v{offered}, this machine speaks "
                    f"v{hub_wire} (schema v{body.get('schema_version')} vs "
                    f"v{state_schema.SCHEMA_VERSION}). The synced row shapes differ, "
                    "so no row may cross; upgrade whichever machine is on the "
                    "lower wire version, then sync again."
                ),
            },
        )

    return app


def serve_wire_mismatch_hub() -> Iterator[str]:
    port = free_port()
    config = uvicorn.Config(
        wire_mismatch_app(), host="127.0.0.1", port=port, log_level="warning"
    )
    server, thread = start_uvicorn_in_thread(config, what="the wire-mismatch hub")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
