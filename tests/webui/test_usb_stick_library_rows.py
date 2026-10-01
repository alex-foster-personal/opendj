"""Per-row truth in the stick library listing: is the audio there, and the play count.

Regression lines:
  - if a listed row says file_present but HEAD /audio answers 404 then the browser offers
    a load that fails with USB_FILE_MISSING
  - if file_present is true for a file deleted from the stick then a broken row reads playable
  - if the listing reports play_count 0 for a played track then the stick's metadata is wrong
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.sync.usb import stick_library as sl
from apps.webui.server.routes import usb_volumes as usb_mod

from tests.sync.usb.export_pdb_builder import write_export_pdb
from tests.sync.usb.synthetic_stick import synthetic_export, synthetic_tracks

from .test_usb_stick_routes import API, VOLUME_ID, _track_url

pytest_plugins = ["tests.webui.test_usb_stick_routes"]

_LIBRARY_URL = f"{API}/volumes/{VOLUME_ID}/library"


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    usb_mod._reset_state_for_tests()
    sl._reset_for_tests()
    yield
    usb_mod._reset_state_for_tests()
    sl._reset_for_tests()


def _rows(client: TestClient) -> dict[int, dict[str, object]]:
    # The volume list is what discovers the stick, as in the app.
    listed = client.get(f"{API}/volumes")
    assert VOLUME_ID in {v["id"] for v in listed.json()["volumes"]}, listed.text
    body = client.get(_LIBRARY_URL)
    assert body.status_code == 200, body.text
    return {row["pdb_id"]: row for row in body.json()["tracks"]}


def _file_present(client: TestClient) -> dict[int, bool]:
    return {pdb_id: row["file_present"] for pdb_id, row in _rows(client).items()}


def test_listing_file_present_predicts_what_the_audio_route_serves(
    client: TestClient, mount: Path
) -> None:
    assert mount.is_dir(), "the synthetic stick is the only volume this test may see"
    present = _file_present(client)
    for pdb_id, listed in present.items():
        served = client.head(_track_url(pdb_id, "/audio")).status_code == 200
        assert listed is served, (pdb_id, "the listing and HEAD /audio disagree")
    # Both answers occur on this stick, so neither side of the check is vacuous:
    # 1 and 2 are real audio; 3 is a .txt, 4 escapes the mount, 5 is a link off
    # the stick, 9 carries a NUL.
    assert {pdb_id for pdb_id, ok in present.items() if not ok} == {3, 4, 5, 9}
    assert present[1] is True and present[2] is True


def test_listing_marks_a_deleted_audio_file_absent_and_keeps_the_rest(
    client: TestClient, mount: Path
) -> None:
    (mount / "Contents" / "second.flac").unlink()
    present = _file_present(client)
    assert present[2] is False, "the export names second.flac, which is no longer on the stick"
    assert client.head(_track_url(2, "/audio")).status_code == 404
    assert present[1] is True, "control: a file still on the stick stays present"
    assert client.head(_track_url(1, "/audio")).status_code == 200


def test_listing_carries_the_exports_play_count(client: TestClient, mount: Path) -> None:
    tracks = [
        dataclasses.replace(track, play_count=7) if track.id == 2 else track
        for track in synthetic_tracks()
    ]
    write_export_pdb(mount, synthetic_export(tracks))
    rows = _rows(client)
    assert rows[2]["play_count"] == 7
    assert rows[1]["play_count"] == 0, "control: an unplayed track stays 0"
