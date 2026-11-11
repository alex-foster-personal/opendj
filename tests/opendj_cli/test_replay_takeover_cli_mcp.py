"""SETREC-12067: replay take-over CLI and MCP parity.

[if] replay take-over lacks CLI/MCP parity [then] agents cannot drive take-over, [else stop].

- if takeover_order accepts junk ids then agents can post nonsense
- if MCP replay_takeover does not match CLI shape then parity breaks
- if the CLI does not surface page failures then operators cannot see take-over errors
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest import mock

import pytest
from httpx import ASGITransport, AsyncClient

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_PAGE
from apps.opendj_cli.__main__ import main
from apps.opendj_cli.mcp_replay_tools import register_replay_tools
from apps.opendj_cli.mcp_sdk import ToolError
from apps.opendj_cli.mcp_server import create_server
from apps.opendj_cli.replay_cli import run as replay_run
from apps.opendj_cli.replay_cli import takeover_order
from tests.opendj_cli.parity_support import _commands_app, http_order_command_async, mcp_tool_command

pytestmark = pytest.mark.requirement("SETREC-12067")


@pytest.mark.parametrize(
    "control",
    ["all", "deck.1.eq.low", "mixer.crossfader"],
)
def test_takeover_order_shapes(control: str) -> None:
    assert takeover_order(control) == {"single": {"type": "replay_takeover", "control": control}}


@pytest.mark.parametrize("control", ["", "deck.5.fader", "eq", "DECK.1.fader"])
def test_takeover_order_refuses_bad_control(control: str) -> None:
    with pytest.raises(ValueError):
        takeover_order(control)


def test_mcp_replay_takeover_posts_side_order() -> None:
    assert mcp_tool_command(
        register_replay_tools,
        "replay_takeover",
        register_kwargs={"tool_error": lambda body: ToolError(str(body.get("message", body)))},
        control="deck.1.eq.low",
    ) == {"type": "replay_takeover", "control": "deck.1.eq.low"}


def test_mcp_replay_takeover_refuses_bad_id() -> None:
    errors: list[dict[str, Any]] = []

    def capture(body: dict[str, Any]) -> ToolError:
        errors.append(body)
        return ToolError(str(body.get("message", "usage")))

    with pytest.raises(ToolError):
        mcp_tool_command(
            register_replay_tools,
            "replay_takeover",
            register_kwargs={"tool_error": capture},
            control="deck.9.x",
        )
    assert errors and errors[0].get("error") == "usage"


@pytest.mark.asyncio
async def test_http_order_offers_replay_takeover() -> None:
    app = _commands_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        command = await http_order_command_async(client, takeover_order("all"))
    assert command == {"type": "replay_takeover", "control": "all"}


def test_cli_replay_takeover_all_confirmed() -> None:
    sent: list[dict[str, Any]] = []

    class FakeClient:
        def post_order(self, order: dict[str, Any]) -> dict[str, Any]:
            sent.append(order)
            return {
                "steps": [
                    {"status": "succeeded", "result": {"taken_over": "all", "live": []}},
                ],
            }

    with (
        mock.patch("apps.opendj_cli.replay_cli.resolve_verified_origin", return_value=object()),
        mock.patch("apps.opendj_cli.replay_cli.EngineClient", return_value=FakeClient()),
    ):
        code = main(["replay", "takeover", "all"])
    assert code == EXIT_CONFIRMED
    assert sent == [takeover_order("all")]


def test_cli_replay_takeover_failed_step(capsys: pytest.CaptureFixture[str]) -> None:
    class FakeClient:
        def post_order(self, order: dict[str, Any]) -> dict[str, Any]:
            return {"steps": [{"status": "failed", "error": "no replay is running"}]}

    with (
        mock.patch("apps.opendj_cli.replay_cli.resolve_verified_origin", return_value=object()),
        mock.patch("apps.opendj_cli.replay_cli.EngineClient", return_value=FakeClient()),
    ):
        code = main(["replay", "takeover", "all"])
    captured = capsys.readouterr()
    assert code == EXIT_FAILED
    assert "no replay is running" in captured.err


def test_cli_replay_takeover_no_page() -> None:
    from apps.opendj_cli.client import NoPerformancePage

    class FakeClient:
        def post_order(self, order: dict[str, Any]) -> dict[str, Any]:
            raise NoPerformancePage("no page")

    with (
        mock.patch("apps.opendj_cli.replay_cli.resolve_verified_origin", return_value=object()),
        mock.patch("apps.opendj_cli.replay_cli.EngineClient", return_value=FakeClient()),
    ):
        code = replay_run(["takeover", "all"])
    assert code == EXIT_NO_PAGE


def test_create_server_lists_replay_takeover_tool() -> None:
    tools = {tool.name for tool in asyncio.run(create_server().list_tools())}
    assert "replay_takeover" in tools
