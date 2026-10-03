"""Stick files are served through the descriptor that was checked (Codex on #4974, 7ee2306e4).

[if] a stick path is swapped for a link between the containment check and the open [then] serving refuses with USB_PATH_OUTSIDE_VOLUME, [else stop].
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from apps.shared.fd_anchored_walk import FD_ANCHORED_WALK_SUPPORTED
from apps.sync.usb.stick_file_pin import pin_stick_file
from apps.sync.usb.stick_model import StickError

pytestmark = [
    pytest.mark.requirement("USBPLAY-06"),
    pytest.mark.skipif(not FD_ANCHORED_WALK_SUPPORTED, reason="no O_NOFOLLOW or fd paths: Windows serves the checked path"),
]

UUID = "0123ABCD-0000-0000-0000-000000000000"


def _stick_and_outside(tmp_path: Path) -> tuple[Path, Path]:
    music = tmp_path / "stick" / "Contents"
    music.mkdir(parents=True)
    (music / "track.mp3").write_bytes(b"stick-audio")
    outside = tmp_path / "host"
    outside.mkdir()
    (outside / "track.mp3").write_bytes(b"host-secret")
    return music, outside


def test_a_directory_swapped_for_a_link_after_the_check_is_refused(tmp_path: Path):
    """[if] a checked file's directory becomes a link out of the mount [then] pinning refuses, [else stop].

    MUTATION TARGET: drop the ``path_from_fd`` comparison and the host file is served.
    """
    music, outside = _stick_and_outside(tmp_path)
    checked = (music / "track.mp3").resolve()
    music.rename(music.with_name("Contents-moved"))
    music.symlink_to(outside, target_is_directory=True)

    with pytest.raises(StickError) as raised:
        pin_stick_file(UUID, checked)
    assert raised.value.code == "USB_PATH_OUTSIDE_VOLUME"


def test_a_leaf_swapped_for_a_link_after_the_check_is_refused(tmp_path: Path):
    """[if] the checked leaf becomes a link [then] the no-follow open refuses it, [else stop]."""
    music, outside = _stick_and_outside(tmp_path)
    checked = (music / "track.mp3").resolve()
    checked.unlink()
    checked.symlink_to(outside / "track.mp3")

    with pytest.raises(StickError) as raised:
        pin_stick_file(UUID, checked)
    assert raised.value.code == "USB_PATH_OUTSIDE_VOLUME"


def test_a_pinned_file_keeps_serving_the_checked_bytes_after_a_later_swap(tmp_path: Path):
    """[if] the path is swapped after pinning [then] the served name still reads the checked file, [else stop].

    Control for the refusals above: an unswapped file pins and serves.
    """
    music, outside = _stick_and_outside(tmp_path)
    checked = (music / "track.mp3").resolve()
    pinned = pin_stick_file(UUID, checked)
    try:
        music.rename(music.with_name("Contents-moved"))
        music.symlink_to(outside, target_is_directory=True)
        with open(pinned.serve_path, "rb") as served:
            assert served.read() == b"stick-audio"
        assert os.stat(pinned.serve_path).st_size == len(b"stick-audio")
    finally:
        pinned.close()
    assert pinned.fd is not None
    with pytest.raises(OSError):
        os.fstat(pinned.fd)


@pytest.mark.parametrize(
    ("range_header", "status"),
    [(None, 200), ("bytes=0-3", 206), ("bytes=500-600", 416), ("garbage", 400)],
)
def test_the_pinned_descriptor_is_released_whatever_the_response(tmp_path: Path, range_header, status):
    """[if] a stick file response ends, a 416 or a malformed Range included [then] its descriptor is closed, [else stop].

    MUTATION TARGET: close the fd in a background task instead of a
    ``finally`` and the 416 and 400 rows leak one fd per request.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.webui.server.routes.usb_tracks_files import _PinnedFileResponse

    music, _outside = _stick_and_outside(tmp_path)
    pins = []
    app = FastAPI()

    @app.get("/f")
    def serve() -> _PinnedFileResponse:
        pinned = pin_stick_file(UUID, (music / "track.mp3").resolve())
        pins.append(pinned)
        return _PinnedFileResponse(pinned, media_type="audio/mpeg")

    headers = {} if range_header is None else {"Range": range_header}
    with TestClient(app) as client:
        response = client.get("/f", headers=headers)

    assert response.status_code == status
    assert pins and pins[0].fd is not None
    with pytest.raises(OSError):
        os.fstat(pins[0].fd)
