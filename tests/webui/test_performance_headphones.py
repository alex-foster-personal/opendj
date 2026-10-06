"""CUEOUT-04: HTTP parity for headphone IPC controls.

[if] POST mix while the performance page is attached [then ⛔] the command
     reaches the page and GET reflects the new mix.
[if] a value outside 0..1 is posted [then ⛔] the endpoint returns 400 with
     the IPC parse text and state is unchanged.
[if] no webview is attached [then ⛔] GET and POST return 503 naming the page.
[if] a new headphone IPC command lacks an HTTP mirror [then ⛔] the parity
     test goes red.

[if] a headphone control posts over HTTP [then] it reaches the page and mirrors back, [else stop].
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.app import create_app
from apps.webui.server.routes.commands import router as commands_router
from apps.webui.server.routes.performance_headphones import (
    HEADPHONE_IPC_ROUTES,
)
from apps.webui.server.routes.performance_headphones import (
    router as headphones_router,
)
from apps.webui.server.routes.state import router as state_router
from tests.opendj_cli.ts_contract import command_fields, headphone_command_fields

pytestmark = pytest.mark.requirement("CUEOUT-04")

_DEFAULT_HEADPHONES: dict[str, Any] = {
    "mix": 0.5,
    "level": 0.5,
    "head_delay_ms": 0,
    "alignment_mode": "hybrid",
    "master_delay_ms": 0,
    "calibration": {
        "step": "idle",
        "cue_latency_ms": None,
        "master_latency_ms": None,
        "offset_ms": None,
        "verify_residual_ms": None,
        "probe": None,
        "error": None,
    },
    "selected_output_device_id": None,
    "selected_master_output_device_id": None,
    "selected_input_device_id": None,
    "output_mode": "practice",
    "outputs": [],
    "inputs": [],
    "supported": False,
    "active": False,
    "error": None,
    "device_access": {
        "status": "not_checked",
        "action": "retry",
        "message": "Audio devices have not been checked yet. Audio plays through the system default output.",
        "detail": None,
        "output_pinning": True,
        "notices": [],
    },
}

_DEFAULT_CHANNELS: dict[str, Any] = {
    "1": {"cue_enabled": False},
    "2": {"cue_enabled": False},
    "3": {"cue_enabled": False},
    "4": {"cue_enabled": False},
}

# Acquire needs a visible user gesture; HTTP cannot grant it.
_ACQUIRE_EXEMPTION = frozenset({"headphone_output_acquire"})

_HEADPHONE_IPC_PREDICATE = frozenset(
    {
        command_type
        for command_type in command_fields()
        if command_type.startswith("headphone_")
        # CUEOUT-15: preview_* joins at `cueSum`, like channel CUE.
        or command_type
        in {
            "channel_cue",
            "output_mode",
            "head_delay_ms",
            "master_delay_ms",
            "preview_cue",
            "preview_stop",
        }
    }
)

# Template helpers in performance-ipc use ``${name}``; check the stable fragments.
_IPC_ERROR_FRAGMENTS = (
    "must be a finite number",
    "must be within 0..1",
    "headphone output_mode must be practice, two_outputs, or split_cable; got",
    "deck must be one of 1, 2, 3, 4; got",
    "must be boolean",
    "device_id must be a non-empty string",
    # CUEOUT-14 (raised from player/constants.ts, imported by both files).
    "headphone alignment_mode must be headphones_only, delay_all, or hybrid; got",
    "master delay must be a finite number within 0..",
)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(state_router, prefix="/api/v1")
    app.include_router(commands_router, prefix="/api/v1")
    app.include_router(headphones_router, prefix="/api/v1")
    return app


async def _open_page(
    client: AsyncClient,
    headphones: dict[str, Any] | None = None,
    channels: dict[str, Any] | None = None,
) -> None:
    published = await client.put(
        "/api/v1/state/ui-mirror",
        json={
            "client_open": True,
            "mixer": {
                "headphones": headphones or dict(_DEFAULT_HEADPHONES),
                "channels": channels or dict(_DEFAULT_CHANNELS),
            },
        },
    )
    assert published.status_code == 202


def _apply_command(mirror: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    """Mutate mirror the way a real page would after executing a command."""
    changed: dict[str, Any] = {}
    command_type = command["type"]
    if command_type == "headphone_mix":
        mirror["mixer"]["headphones"]["mix"] = command["value"]
        changed = {"mixer": {"headphones": {"mix": command["value"]}}}
    elif command_type == "headphone_level":
        mirror["mixer"]["headphones"]["level"] = command["value"]
        changed = {"mixer": {"headphones": {"level": command["value"]}}}
    elif command_type == "head_delay_ms":
        mirror["mixer"]["headphones"]["head_delay_ms"] = command["value"]
        changed = {"mixer": {"headphones": {"head_delay_ms": command["value"]}}}
    elif command_type == "headphone_alignment_mode":
        mirror["mixer"]["headphones"]["alignment_mode"] = command["value"]
        changed = {"mixer": {"headphones": {"alignment_mode": command["value"]}}}
    elif command_type == "master_delay_ms":
        mirror["mixer"]["headphones"]["master_delay_ms"] = command["value"]
        changed = {"mixer": {"headphones": {"master_delay_ms": command["value"]}}}
    elif command_type == "headphone_calibrate":
        calibration = {
            "step": "applied",
            "cue_latency_ms": 900,
            "master_latency_ms": 200,
            "offset_ms": 700,
            "verify_residual_ms": 1.5,
            "probe": None,
            "error": None,
        }
        mirror["mixer"]["headphones"]["calibration"] = calibration
        mirror["mixer"]["headphones"]["master_delay_ms"] = 700
        changed = {"mixer": {"headphones": {"calibration": calibration, "master_delay_ms": 700}}}
    elif command_type == "headphone_calibrate_abort":
        calibration = dict(_DEFAULT_HEADPHONES["calibration"])
        mirror["mixer"]["headphones"]["calibration"] = calibration
        changed = {"mixer": {"headphones": {"calibration": calibration}}}
    elif command_type == "channel_cue":
        deck = str(command["deck"])
        mirror["mixer"]["channels"][deck]["cue_enabled"] = command["enabled"]
        changed = {
            "mixer": {
                "channels": {deck: {"cue_enabled": command["enabled"]}},
            },
        }
    elif command_type == "output_mode":
        mirror["mixer"]["headphones"]["output_mode"] = command["mode"]
        changed = {"mixer": {"headphones": {"output_mode": command["mode"]}}}
    elif command_type == "headphone_outputs_refresh":
        mirror["mixer"]["headphones"]["outputs"] = [
            {"id": "default", "label": "Default"},
        ]
        mirror["mixer"]["headphones"]["inputs"] = [
            {"id": "builtin-mic", "label": "MacBook Pro Microphone"},
        ]
        changed = {
            "mixer": {
                "headphones": {
                    "outputs": [{"id": "default", "label": "Default"}],
                    "inputs": [
                        {"id": "builtin-mic", "label": "MacBook Pro Microphone"},
                    ],
                },
            },
        }
    elif command_type == "headphone_output_select":
        mirror["mixer"]["headphones"]["selected_output_device_id"] = command["device_id"]
        changed = {
            "mixer": {
                "headphones": {
                    "selected_output_device_id": command["device_id"],
                },
            },
        }
    elif command_type == "headphone_master_select":
        mirror["mixer"]["headphones"]["selected_master_output_device_id"] = command["device_id"]
        changed = {
            "mixer": {
                "headphones": {
                    "selected_master_output_device_id": command["device_id"],
                },
            },
        }
    elif command_type == "headphone_input_select":
        mirror["mixer"]["headphones"]["selected_input_device_id"] = command["device_id"]
        changed = {
            "mixer": {
                "headphones": {
                    "selected_input_device_id": command["device_id"],
                },
            },
        }
    return changed


async def _fake_page(
    client: AsyncClient,
    apply: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Claim the next order, apply it to the mirror, and complete it."""
    mirror: dict[str, Any] = {
        "client_open": True,
        "mixer": {
            "headphones": dict(_DEFAULT_HEADPHONES),
            "channels": dict(_DEFAULT_CHANNELS),
        },
    }
    claimed: dict[str, Any] | None = None
    for _ in range(200):
        await asyncio.sleep(0.01)
        nxt = await client.get("/api/v1/commands/next")
        assert nxt.status_code == 200
        payload = nxt.json()
        if payload is not None:
            claimed = payload
            break
    assert claimed is not None, "the open page was never offered the order"
    command = claimed["payload"]
    changed = _apply_command(mirror, command)
    if apply is not None:
        override = apply(mirror, command)
        if override is not None:
            changed = override
    await client.put("/api/v1/state/ui-mirror", json=mirror)
    await client.post(
        f"/api/v1/commands/{claimed['id']}/result",
        json={
            "steps": [{"status": "succeeded"}],
            "mirror_delta": {"changed": changed},
        },
    )
    return command


def test_headphone_command_union_matches_route_table() -> None:
    assert set(headphone_command_fields()) == set(HEADPHONE_IPC_ROUTES)


def test_performance_headphone_predicate_matches_headphone_command_union() -> None:
    assert set(headphone_command_fields()) == _HEADPHONE_IPC_PREDICATE - _ACQUIRE_EXEMPTION


def test_ipc_error_strings_appear_in_performance_ipc_source() -> None:
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    ipc_source = (root / "apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts").read_text(
        encoding="utf-8"
    )
    headphones_source = (root / "apps/webui/frontend/src/lib/player/headphones.ts").read_text(
        encoding="utf-8"
    )
    # The mode and delay contracts live in the leaf constants module (CUEOUT-14
    # moved output_mode there so the alignment policy can import it cycle-free).
    constants_source = (root / "apps/webui/frontend/src/lib/player/constants.ts").read_text(
        encoding="utf-8"
    )
    combined = ipc_source + headphones_source + constants_source
    assert "assertHeadphoneOutputMode" in ipc_source
    for fragment in _IPC_ERROR_FRAGMENTS:
        assert fragment in combined


def test_mix_happy_path() -> None:
    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client)
            fake = asyncio.create_task(_fake_page(client))
            response = await client.post("/api/v1/performance/headphones/mix", json={"value": 0.25})
            command = await fake
            assert command == {"type": "headphone_mix", "value": 0.25}
            assert response.status_code == 200
            assert response.json()["mix"] == 0.25
            got = await client.get("/api/v1/performance/headphones")
            assert got.status_code == 200
            assert got.json()["mix"] == 0.25

    asyncio.run(run())


def test_mix_out_of_range_is_rejected_without_submitting() -> None:
    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client)
            too_high = await client.post("/api/v1/performance/headphones/mix", json={"value": 1.5})
            too_low = await client.post("/api/v1/performance/headphones/mix", json={"value": -0.1})
            not_finite = await client.post(
                "/api/v1/performance/headphones/mix", json={"value": None}
            )
            nxt = await client.get("/api/v1/commands/next")
            got = await client.get("/api/v1/performance/headphones")
        assert too_high.status_code == 400
        assert too_high.json()["detail"] == "value must be within 0..1"
        assert too_low.status_code == 400
        assert too_low.json()["detail"] == "value must be within 0..1"
        assert not_finite.status_code == 400
        assert not_finite.json()["detail"] == "value must be a finite number"
        assert nxt.json() is None
        assert got.json()["mix"] == 0.5

    asyncio.run(run())


def test_unknown_output_mode_is_rejected() -> None:
    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client)
            response = await client.post(
                "/api/v1/performance/headphones/output-mode",
                json={"mode": "nope"},
            )
            got = await client.get("/api/v1/performance/headphones")
        assert response.status_code == 400
        assert (
            response.json()["detail"]
            == "headphone output_mode must be practice, two_outputs, or split_cable; got nope"
        )
        assert got.json()["output_mode"] == "practice"

    asyncio.run(run())


def test_no_page_returns_503() -> None:
    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            got = await client.get("/api/v1/performance/headphones")
            posted = await client.post("/api/v1/performance/headphones/mix", json={"value": 0.25})
        assert got.status_code == 503
        assert "performance page" in got.json()["detail"]
        assert posted.status_code == 503
        assert "performance page" in posted.json()["detail"]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("path", "body", "expected_command"),
    [
        (
            "/api/v1/performance/headphones/level",
            {"value": 0.75},
            {"type": "headphone_level", "value": 0.75},
        ),
        (
            "/api/v1/performance/headphones/channel-cue",
            {"deck": 2, "enabled": True},
            {"type": "channel_cue", "deck": 2, "enabled": True},
        ),
        (
            "/api/v1/performance/headphones/outputs/refresh",
            {},
            {"type": "headphone_outputs_refresh"},
        ),
        (
            "/api/v1/performance/headphones/outputs/select",
            {"device_id": "usb-audio-1"},
            {"type": "headphone_output_select", "device_id": "usb-audio-1"},
        ),
        (
            "/api/v1/performance/headphones/outputs/master",
            {"device_id": "speakers"},
            {"type": "headphone_master_select", "device_id": "speakers"},
        ),
        (
            "/api/v1/performance/headphones/inputs/select",
            {"device_id": "builtin-mic"},
            {"type": "headphone_input_select", "device_id": "builtin-mic"},
        ),
        (
            "/api/v1/performance/headphones/alignment-mode",
            {"value": "delay_all"},
            {"type": "headphone_alignment_mode", "value": "delay_all"},
        ),
        (
            "/api/v1/performance/headphones/master-delay",
            {"value": 700},
            {"type": "master_delay_ms", "value": 700.0},
        ),
        (
            "/api/v1/performance/headphones/calibrate",
            {},
            {"type": "headphone_calibrate"},
        ),
        (
            "/api/v1/performance/headphones/calibrate/abort",
            {},
            {"type": "headphone_calibrate_abort"},
        ),
        (
            "/api/v1/performance/headphones/preview",
            {"stable_id": "trk-9", "ratio": 0.25},
            {"type": "preview_cue", "stable_id": "trk-9", "ratio": 0.25},
        ),
        (
            "/api/v1/performance/headphones/preview/stop",
            {},
            {"type": "preview_stop"},
        ),
    ],
)
def test_headphone_post_routes_reach_the_page(
    path: str, body: dict[str, Any], expected_command: dict[str, Any]
) -> None:
    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client)
            fake = asyncio.create_task(_fake_page(client))
            response = await client.post(path, json=body)
            command = await fake
            assert command == expected_command
            assert response.status_code == 200

    asyncio.run(run())


@pytest.mark.parametrize(
    ("path", "body", "detail"),
    [
        (
            "/api/v1/performance/headphones/alignment-mode",
            {"value": "serato"},
            "headphone alignment_mode must be headphones_only, delay_all, or hybrid; got serato",
        ),
        (
            "/api/v1/performance/headphones/master-delay",
            {"value": 1501},
            "master delay must be a finite number within 0..1500, got 1501",
        ),
        (
            "/api/v1/performance/headphones/master-delay",
            {"value": -1},
            "master delay must be a finite number within 0..1500, got -1",
        ),
        (
            "/api/v1/performance/headphones/master-delay",
            {"value": "700"},
            "master delay must be a finite number within 0..1500, got 700",
        ),
    ],
)
def test_alignment_bodies_are_rejected_without_submitting(
    path: str, body: dict[str, Any], detail: str
) -> None:
    """CUEOUT-14: a bad mode or an out-of-range room delay never reaches the page."""

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client)
            response = await client.post(path, json=body)
            nxt = await client.get("/api/v1/commands/next")
        assert response.status_code == 400
        assert response.json()["detail"] == detail
        assert nxt.json() is None

    asyncio.run(run())


def test_empty_device_id_is_rejected_without_submitting() -> None:
    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client)
            response = await client.post(
                "/api/v1/performance/headphones/outputs/select",
                json={"device_id": ""},
            )
            nxt = await client.get("/api/v1/commands/next")
        assert response.status_code == 400
        assert response.json()["detail"] == "device_id must be a non-empty string"
        assert nxt.json() is None

    asyncio.run(run())


def test_failed_page_step_returns_400_and_leaves_selection_unchanged() -> None:
    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(
                client,
                headphones={
                    **_DEFAULT_HEADPHONES,
                    "selected_output_device_id": "existing",
                },
            )

            async def claim_and_fail() -> None:
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    nxt = await client.get("/api/v1/commands/next")
                    if nxt.json() is not None:
                        claimed = nxt.json()
                        break
                else:
                    raise AssertionError("order was never offered")
                await client.post(
                    f"/api/v1/commands/{claimed['id']}/result",
                    json={
                        "steps": [
                            {
                                "status": "failed",
                                "error": (
                                    "headphone output usb-x is not an enumerated headphone output"
                                ),
                            }
                        ],
                        "mirror_delta": {"changed": {}},
                    },
                )

            fake = asyncio.create_task(claim_and_fail())
            response = await client.post(
                "/api/v1/performance/headphones/outputs/select",
                json={"device_id": "usb-x"},
            )
            await fake
            got = await client.get("/api/v1/performance/headphones")

        assert response.status_code == 400
        assert (
            response.json()["detail"]
            == "headphone output usb-x is not an enumerated headphone output"
        )
        assert got.json()["selected_output_device_id"] == "existing"

    asyncio.run(run())


def test_create_app_mounts_every_headphone_route(client) -> None:
    paths = create_app(mount_frontend=False).openapi()["paths"]
    assert "/api/v1/performance/headphones" in paths
    for method, route in HEADPHONE_IPC_ROUTES.values():
        assert route in paths
        assert method.lower() in paths[route]


@pytest.mark.requirement("CUEOUT-26")
def test_get_headphones_carries_the_same_device_split_and_the_jack_fields() -> None:
    """[if] the page mirrors a same-device split [then] GET shows split_reason and the muted output, [else stop]."""

    async def run() -> None:
        headphones = dict(_DEFAULT_HEADPHONES)
        headphones.update(
            {
                "output_mode": "split_cable",
                "split_reason": "same_device",
                "selected_master_output_device_id": "native:BuiltInHeadphoneOutputDevice",
                "outputs": [
                    {
                        "id": "native:BuiltInSpeakerDevice",
                        "label": "MacBook Pro Speakers",
                        "physical_id": "native:BuiltInHeadphoneOutputDevice",
                        "muted_by_jack": True,
                        "transport": "builtin",
                    },
                    {
                        "id": "native:BuiltInHeadphoneOutputDevice",
                        "label": "External Headphones",
                        "physical_id": "native:BuiltInHeadphoneOutputDevice",
                        "muted_by_jack": False,
                        "transport": "builtin",
                    },
                ],
            }
        )
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
            await _open_page(client, headphones=headphones)
            got = await client.get("/api/v1/performance/headphones")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["split_reason"] == "same_device"
        assert body["output_mode"] == "split_cable"
        speakers = body["outputs"][0]
        assert speakers["muted_by_jack"] is True
        assert speakers["physical_id"] == "native:BuiltInHeadphoneOutputDevice"

    asyncio.run(run())


@pytest.mark.requirement("CUEOUT-26")
def test_control_operator_state_has_no_split_reason() -> None:
    """[if] the page never auto-split [then] split_reason is null, [else stop]."""

    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
            await _open_page(client)
            got = await client.get("/api/v1/performance/headphones")
        assert got.status_code == 200, got.text
        assert got.json()["split_reason"] is None

    asyncio.run(run())
