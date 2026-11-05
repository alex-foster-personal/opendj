"""[if] AIFC COMM FourCC is outside the accepted set [then] reject naming it, [else stop]."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.audio_playable import UnplayableAudioError, probe_playable_audio
from tests.shared.audio_probe_fixtures import (
    aifc_comm,
    write_aifc,
    write_aiff,
    write_aiff_post_ssnd_truncated_header,
    write_form_type,
)

pytestmark = pytest.mark.requirement("SETUP-16")


def _reject(path: Path, token: str) -> None:
    with pytest.raises(UnplayableAudioError) as caught:
        probe_playable_audio(path)
    assert token in caught.value.reason


def test_aiff_comm_ssnd_imports(tmp_path: Path) -> None:
    """[if] an AIFF has COMM and SSND [then] it imports, [else stop]."""
    probe_playable_audio(write_aiff(tmp_path / "ok.aiff"))


def test_aifc_sowt_imports(tmp_path: Path) -> None:
    """[if] AIFC COMM FourCC is sowt [then] it imports, [else stop]."""
    probe_playable_audio(write_aifc(tmp_path / "sowt.aiff", b"sowt"))


def test_aifc_fl32_imports(tmp_path: Path) -> None:
    """[if] AIFC COMM FourCC is fl32 [then] it imports, [else stop]."""
    probe_playable_audio(write_aifc(tmp_path / "fl32.aiff", b"fl32", sample_size=32))


def test_unknown_fourcc_rejects_by_name(tmp_path: Path) -> None:
    """[if] AIFC FourCC is unknown [then] reject naming it, [else stop]."""
    _reject(write_aifc(tmp_path / "xxxx.aiff", b"XXXX"), "unknown aifc compression")


def test_ima4_rejects_as_unsupported(tmp_path: Path) -> None:
    """[if] AIFC FourCC is ima4 [then] reject naming it, [else stop]."""
    _reject(write_aifc(tmp_path / "ima4.aiff", b"ima4"), "unsupported aifc compression")


def test_pascal_name_overrun_rejects(tmp_path: Path) -> None:
    """[if] COMM Pascal name overruns the chunk [then] reject, [else stop]."""
    comm = aifc_comm(b"sowt", include_name=False) + bytes([40])
    _reject(write_aifc(tmp_path / "pascal.aiff", b"sowt", comm_payload=comm), "truncated")


def test_aifc_comm_size_18_rejects(tmp_path: Path) -> None:
    """[if] AIFC COMM is shorter than 22 bytes [then] probe rejects, [else stop]."""
    comm = aifc_comm(b"sowt")[:18]
    _reject(write_aifc(tmp_path / "short-comm.aiff", b"sowt", comm_payload=comm), "truncated")


def test_missing_ssnd_rejects(tmp_path: Path) -> None:
    """[if] an AIFF has COMM and no SSND [then] probe rejects, [else stop]."""
    _reject(write_aifc(tmp_path / "no-ssnd.aiff", b"sowt", include_ssnd=False), "SSND")


def test_post_ssnd_truncated_header_rejects(tmp_path: Path) -> None:
    """[if] a chunk header after SSND crosses the container end [then] reject, [else stop]."""
    _reject(write_aiff_post_ssnd_truncated_header(tmp_path / "trail.aiff"), "truncated")


def test_form_type_xxxx_rejects(tmp_path: Path) -> None:
    """[if] FORM type is not AIFF or AIFC [then] probe rejects, [else stop]."""
    _reject(write_form_type(tmp_path / "xxxx.aiff", b"XXXX"), "form type")
