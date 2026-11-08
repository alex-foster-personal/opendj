"""The edit CLI is an exact HTTP twin of the UI.

[if] an edit verb is requested [then] it maps to the guarded edit route, [else stop].
"""

from __future__ import annotations

import argparse

import pytest

from apps.opendj_cli import edit_cli

pytestmark = pytest.mark.requirement("LIBEDIT-9179")


@pytest.mark.parametrize(
    ("argv", "method", "path", "body"),
    [
        (
            [
                "create",
                "--source",
                "source-1",
                "--label",
                "Cut",
                "--in-ms",
                "1000",
                "--out-ms",
                "9000",
                "--fx",
                '{"echo":0.2}',
                "--playlist",
                "p1",
            ],
            "POST",
            "/api/v1/edits",
            {
                "source_track_id": "source-1",
                "label": "Cut",
                "in_ms": 1_000,
                "out_ms": 9_000,
                "fx": {"echo": 0.2},
                "playlist_id": "p1",
            },
        ),
        (
            ["update", "edit/1", "--label", "Short", "--out-ms", "7000"],
            "PATCH",
            "/api/v1/edits/edit%2F1",
            {"label": "Short", "out_ms": 7_000},
        ),
        (["rm", "edit/1"], "DELETE", "/api/v1/edits/edit%2F1", None),
    ],
)
def test_verbs_map_to_edit_routes(argv: list[str], method: str, path: str, body: dict[str, object] | None) -> None:
    request, session = edit_cli.build_request(argv)
    assert (request.method, request.path, request.body) == (method, path, body)
    assert session == edit_cli.DEFAULT_UNDO_SESSION


def test_update_omits_fields_the_caller_did_not_set() -> None:
    request, _session = edit_cli.build_request(["update", "e1", "--in-ms", "500"])
    assert request.body == {"in_ms": 500}


def test_create_rejects_non_object_fx() -> None:
    with pytest.raises(argparse.ArgumentError):
        edit_cli.build_request(
            [
                "create",
                "--source",
                "s",
                "--in-ms",
                "0",
                "--out-ms",
                "1",
                "--fx",
                "[]",
            ]
        )
