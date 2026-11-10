"""SECONDSCREEN-10757: ``opendj window open|close|focus`` posts a window command.

[if] open succeeds [then] the CLI confirms the order result, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED
from apps.opendj_cli.__main__ import main

pytestmark = pytest.mark.requirement("SECONDSCREEN-10757")


def _lock(tmp_path: Path) -> Path:
    return tmp_path / ".engine.lock"


def test_unknown_window_label_is_refused_with_the_known_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the label is unknown [then] the CLI names the known list, [else stop]."""
    code = main(["--json", "--lock", str(_lock(tmp_path)), "window", "open", "booth"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_FAILED
    assert payload["error"]["code"] == "unknown_window_label"
    assert "second-screen" in payload["error"]["message"]
    assert "booth" in payload["error"]["message"]


def test_window_open_without_a_label_is_usage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] open is sent with no label [then] the CLI prints usage, [else stop]."""
    assert main(["--json", "--lock", str(_lock(tmp_path)), "window", "open"]) == EXIT_FAILED
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_catalog_window_verb_builds_the_bus_command() -> None:
    """[if] MCP uses verb window [then] the catalog builds the bus command, [else stop]."""
    from apps.opendj_cli.catalog import VERBS
    from apps.opendj_cli.verbs import parse_invocation

    assert "window" in VERBS, "MCP command(verb=window) reads the catalog as its single source"
    assert parse_invocation(["window", "open", "second-screen"]).command == {
        "type": "window",
        "action": "open",
        "label": "second-screen",
    }


def test_open_posts_the_command_and_confirms_the_order_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] open succeeds [then] the CLI confirms the order result, [else stop]."""
    from apps.opendj_cli import window_cli

    posted: list[object] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def post_order(self, order: object) -> dict[str, object]:
            posted.append(order)
            return {"steps": [{"status": "succeeded"}]}

        def mirror(self) -> dict[str, object]:
            raise AssertionError("must not poll the UI mirror after a successful step")

    monkeypatch.setattr(window_cli, "resolve_origin", lambda lock: object())
    monkeypatch.setattr(window_cli, "EngineClient", FakeClient)
    code = main(["--json", "--lock", str(_lock(tmp_path)), "window", "open", "second-screen"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_CONFIRMED, payload
    assert posted == [{"single": {"type": "window", "action": "open", "label": "second-screen"}}]
    assert payload == {"action": "open", "label": "second-screen"}


def test_close_confirms_the_order_result_without_polling_the_mirror(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] close succeeds [then] the CLI confirms the order result, [else stop]."""
    from apps.opendj_cli import window_cli

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def post_order(self, order: object) -> dict[str, object]:
            return {"steps": [{"status": "succeeded"}]}

        def mirror(self) -> dict[str, object]:
            raise AssertionError("must not poll the UI mirror after a successful step")

    monkeypatch.setattr(window_cli, "resolve_origin", lambda lock: object())
    monkeypatch.setattr(window_cli, "EngineClient", FakeClient)
    code = main(["--json", "--lock", str(_lock(tmp_path)), "window", "close", "second-screen"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_CONFIRMED, payload
    assert payload == {"action": "close", "label": "second-screen"}


def test_a_failed_page_step_is_not_treated_as_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the page refuses with no shell [then] the CLI fails and does not wait, [else stop]."""
    from apps.opendj_cli import window_cli

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def post_order(self, order: object) -> dict[str, object]:
            return {
                "steps": [
                    {
                        "status": "failed",
                        "error": "no desktop shell: the window command needs the Open DJ desktop shell",
                    }
                ]
            }

        def mirror(self) -> dict[str, object]:
            raise AssertionError("must not wait after a failed step")

    monkeypatch.setattr(window_cli, "resolve_origin", lambda lock: object())
    monkeypatch.setattr(window_cli, "EngineClient", FakeClient)
    code = main(["--json", "--lock", str(_lock(tmp_path)), "window", "open", "second-screen"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_FAILED
    assert payload["error"]["code"] == "window_command_failed"
    assert "desktop shell" in payload["error"]["message"]
