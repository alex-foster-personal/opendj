"""POST /api/v1/update/apply and shell command delivery (issue #2924)."""
from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.engine_core.build_info import BUILD_IDENTITY_STATE_ATTR, BuildIdentity, BuildInfoOut
from apps.engine_core.update_channel import (
    UPDATE_APPLY_PATH,
    UPDATE_APPLY_STATUS_PATH,
    UPDATE_ENDPOINT,
    add_update_apply_route,
    add_update_check_route,
)
from apps.webui.server.routes.commands import router as commands_router
from apps.webui.server.shell_commands import shell_broker

KEY = "darwin-aarch64"
RUNNING_VERSION = "0.1.0"
RUNNING_SHA_FULL = "0d41a28c0000000000000000000000000000beef"


def _identity(source: str = "payload", app_version: str | None = RUNNING_VERSION) -> BuildIdentity:
    return BuildIdentity(
        info=BuildInfoOut(
            source=source,
            engine_version="0.1.0",
            git_sha="0d41a28c",
            git_sha_full=RUNNING_SHA_FULL,
            git_branch="main",
            git_dirty=False,
            built_at_utc="2026-08-31T12:00:00Z",
            built_at_kind="payload-build" if source == "payload" else "engine-start",
            app_version=app_version,
        ),
        failure=None,
    )


def _manifest(version: str) -> dict[str, object]:
    return {
        "version": version,
        "notes": "",
        "pub_date": "2026-09-01T00:00:00Z",
        "platforms": {
            KEY: {
                "signature": "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZw==",
                "url": f"https://example.invalid/OpenDJ-{version}.app.tar.gz",
            }
        },
    }


def _app(identity: BuildIdentity) -> FastAPI:
    app = FastAPI()
    setattr(app.state, BUILD_IDENTITY_STATE_ATTR, identity)
    add_update_check_route(app)
    add_update_apply_route(app)
    app.include_router(commands_router, prefix="/api/v1")
    return app


def _patch_channel(monkeypatch, transport: httpx.MockTransport) -> None:
    import apps.engine_core.update_channel as module

    original = module.httpx.Client

    def client_factory(*args, **kwargs):
        if not args and not kwargs:
            return httpx.Client(transport=transport)
        return original(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "Client", client_factory)
    monkeypatch.setattr(module, "platform_key", lambda *a, **k: KEY)


def test_apply_returns_202_and_shell_next_claims_apply_update(monkeypatch) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_manifest("0.1.1"))
        if request.url == UPDATE_ENDPOINT
        else httpx.Response(404)
    )
    _patch_channel(monkeypatch, transport)

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(_identity())), base_url="http://test"
        ) as client:
            apply = await client.post(UPDATE_APPLY_PATH)
            assert apply.status_code == 202
            body = apply.json()
            assert body["accepted"] is True
            assert body["available_version"] == "0.1.1"
            assert isinstance(body["command_id"], str)

            nxt = await client.get("/api/v1/commands/next", params={"consumer": "shell"})
            assert nxt.status_code == 200
            claimed = nxt.json()
            assert claimed == {
                "id": body["command_id"],
                "kind": "shell",
                "command": {"type": "apply-update", "available_version": "0.1.1"},
            }

            again = await client.get("/api/v1/commands/next", params={"consumer": "shell"})
            assert again.status_code == 200
            assert again.json() is None

    asyncio.run(run())


def test_apply_refuses_up_to_date_with_409_and_enqueues_nothing(monkeypatch) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_manifest(RUNNING_VERSION))
        if request.url == UPDATE_ENDPOINT
        else httpx.Response(404)
    )
    _patch_channel(monkeypatch, transport)

    async def run() -> None:
        app = _app(_identity())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            apply = await client.post(UPDATE_APPLY_PATH)
            assert apply.status_code == 409
            refused = apply.json()
            assert refused["status"] == "up-to-date"
            assert isinstance(refused["detail"], str) and refused["detail"] != ""

            nxt = await client.get("/api/v1/commands/next", params={"consumer": "shell"})
            assert nxt.json() is None
            request = type("Req", (), {"app": app})()
            assert shell_broker(request).pending_count() == 0

    asyncio.run(run())


def test_apply_refuses_endpoint_fault_with_409(monkeypatch) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404, text="nope"))
    _patch_channel(monkeypatch, transport)

    async def run() -> None:
        app = _app(_identity())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            apply = await client.post(UPDATE_APPLY_PATH)
            assert apply.status_code == 409
            assert apply.json()["status"] == "endpoint-refused"
            nxt = await client.get("/api/v1/commands/next", params={"consumer": "shell"})
            assert nxt.json() is None

    asyncio.run(run())


def test_apply_refuses_repo_checkout_with_409(monkeypatch) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_manifest("0.1.1"))
        if request.url == UPDATE_ENDPOINT
        else httpx.Response(404)
    )
    _patch_channel(monkeypatch, transport)

    async def run() -> None:
        app = _app(_identity(source="repo"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            apply = await client.post(UPDATE_APPLY_PATH)
            assert apply.status_code == 409
            refused = apply.json()
            assert refused["status"] == "identity-unavailable"
            assert "not a packaged build" in refused["detail"]
            nxt = await client.get("/api/v1/commands/next", params={"consumer": "shell"})
            assert nxt.json() is None

    asyncio.run(run())


def test_second_apply_while_first_pending_returns_409(monkeypatch) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_manifest("0.1.1"))
        if request.url == UPDATE_ENDPOINT
        else httpx.Response(404)
    )
    _patch_channel(monkeypatch, transport)

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(_identity())), base_url="http://test"
        ) as client:
            first = await client.post(UPDATE_APPLY_PATH)
            assert first.status_code == 202
            command_id = first.json()["command_id"]

            second = await client.post(UPDATE_APPLY_PATH)
            assert second.status_code == 409
            detail = second.json()["detail"]
            assert command_id in detail

    asyncio.run(run())


def _apply_status_path(command_id: str) -> str:
    return UPDATE_APPLY_STATUS_PATH.format(command_id=command_id)


def test_apply_status_pending_before_claim(monkeypatch) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_manifest("0.1.1"))
        if request.url == UPDATE_ENDPOINT
        else httpx.Response(404)
    )
    _patch_channel(monkeypatch, transport)

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(_identity())), base_url="http://test"
        ) as client:
            apply = await client.post(UPDATE_APPLY_PATH)
            assert apply.status_code == 202
            command_id = apply.json()["command_id"]

            status = await client.get(_apply_status_path(command_id))
            assert status.status_code == 200
            body = status.json()
            assert body["command_id"] == command_id
            assert body["state"] == "pending"
            assert body["outcome"] is None
            assert body["error"] is None
            assert isinstance(body["enqueued_at_utc"], str) and body["enqueued_at_utc"] != ""
            assert body["claimed_at_utc"] is None
            assert body["completed_at_utc"] is None

    asyncio.run(run())


def test_apply_status_claimed_after_shell_next(monkeypatch) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_manifest("0.1.1"))
        if request.url == UPDATE_ENDPOINT
        else httpx.Response(404)
    )
    _patch_channel(monkeypatch, transport)

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(_identity())), base_url="http://test"
        ) as client:
            apply = await client.post(UPDATE_APPLY_PATH)
            command_id = apply.json()["command_id"]

            nxt = await client.get("/api/v1/commands/next", params={"consumer": "shell"})
            assert nxt.status_code == 200

            status = await client.get(_apply_status_path(command_id))
            assert status.status_code == 200
            body = status.json()
            assert body["state"] == "claimed"
            assert isinstance(body["claimed_at_utc"], str) and body["claimed_at_utc"] != ""
            assert body["completed_at_utc"] is None
            assert body["outcome"] is None
            assert body["error"] is None

    asyncio.run(run())


def test_apply_status_failed_after_shell_result(monkeypatch, caplog) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_manifest("0.1.1"))
        if request.url == UPDATE_ENDPOINT
        else httpx.Response(404)
    )
    _patch_channel(monkeypatch, transport)

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(_identity())), base_url="http://test"
        ) as client:
            apply = await client.post(UPDATE_APPLY_PATH)
            command_id = apply.json()["command_id"]

            nxt = await client.get("/api/v1/commands/next", params={"consumer": "shell"})
            assert nxt.status_code == 200

            with caplog.at_level(logging.WARNING, logger="apps.webui.server.shell_commands"):
                result = await client.post(
                    f"/api/v1/commands/{command_id}/result",
                    json={
                        "status": "failed",
                        "outcome": "refused",
                        "error": "minisign verify failed",
                    },
                )
            assert result.status_code == 202

            status = await client.get(_apply_status_path(command_id))
            assert status.status_code == 200
            body = status.json()
            assert body["state"] == "failed"
            assert body["outcome"] == "refused"
            assert body["error"] == "minisign verify failed"
            assert isinstance(body["completed_at_utc"], str) and body["completed_at_utc"] != ""

            warnings = [
                record
                for record in caplog.records
                if record.levelname == "WARNING"
                and "shell apply-update" in record.getMessage()
            ]
            assert len(warnings) == 1
            message = warnings[0].getMessage()
            assert command_id in message
            assert "refused" in message
            assert "minisign verify failed" in message

    asyncio.run(run())


def test_apply_status_succeeded_after_shell_result(monkeypatch) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_manifest("0.1.1"))
        if request.url == UPDATE_ENDPOINT
        else httpx.Response(404)
    )
    _patch_channel(monkeypatch, transport)

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(_identity())), base_url="http://test"
        ) as client:
            apply = await client.post(UPDATE_APPLY_PATH)
            command_id = apply.json()["command_id"]

            nxt = await client.get("/api/v1/commands/next", params={"consumer": "shell"})
            assert nxt.status_code == 200

            result = await client.post(
                f"/api/v1/commands/{command_id}/result",
                json={"status": "succeeded", "outcome": "installed"},
            )
            assert result.status_code == 202

            status = await client.get(_apply_status_path(command_id))
            assert status.status_code == 200
            body = status.json()
            assert body["state"] == "succeeded"
            assert body["outcome"] == "installed"
            assert body["error"] is None
            assert isinstance(body["completed_at_utc"], str) and body["completed_at_utc"] != ""

    asyncio.run(run())


def test_apply_status_404_unknown_id() -> None:
    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(_identity())), base_url="http://test"
        ) as client:
            status = await client.get(_apply_status_path("deadbeefdeadbeefdeadbeefdeadbeef"))
            assert status.status_code == 404

    asyncio.run(run())
