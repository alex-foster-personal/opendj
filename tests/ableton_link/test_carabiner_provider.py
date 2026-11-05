"""LINK-124: the pinned Carabiner is verified the same way for the packaged app and ``just link-up``.

Everything Linux can run is real: the download is served over a loopback HTTP server and hashed,
an unreachable URL is a real connection failure, and the signature verdict reads real
``codesign -dv`` output text. ``hdiutil`` and ``codesign`` themselves only exist on a Mac, so the
install-from-DMG step is listed as Mac-only in the PR.

- [if] bytes that do not hash to the pin are accepted [then] fail
- [if] an unreachable download is anything but ``download_failed`` naming the URL [then] fail
- [if] a TeamIdentifier that merely CONTAINS Deep Symmetry's team is accepted [then] fail
- [if] a failed ``codesign --verify`` passes because the team line matches [then] fail
- [if] the install runs on a host other than macOS [then] fail
- [if] ``scripts/carabiner_sidecar.py`` stops being a thin CLI over the same install [then] fail, [else stop].
"""

from __future__ import annotations

import hashlib
import http.server
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.ableton_link import carabiner_provider as provider
from apps.ableton_link.carabiner_provider import (
    DEEP_SYMMETRY_TEAM_ID,
    CarabinerProvisionError,
    fetch_verified_dmg,
    install_pinned_carabiner,
    signature_problem,
    team_identifier,
    verify_sha256,
)

pytestmark = [pytest.mark.requirement("LINK-124")]

PAYLOAD = b"not really a DMG, but real bytes with a real hash\n" * 64
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()

# What `codesign -dv` prints for the pinned binary (stderr), abridged to the lines that matter.
CODESIGN_DETAIL = (
    "Executable=/Users/dev/.local/opendj/carabiner/1.2.0/Carabiner\n"
    "Identifier=org.deepsymmetry.Carabiner\n"
    "Format=Mach-O universal (x86_64 arm64)\n"
    "CodeDirectory v=20500 size=6061 flags=0x10000(runtime) hashes=178+7 location=embedded\n"
    "Signature size=9046\n"
    "Timestamp=Jun 29, 2023 at 9:14:02 PM\n"
    f"TeamIdentifier={DEEP_SYMMETRY_TEAM_ID}\n"
    "Runtime Version=13.3.0\n"
)


@pytest.fixture
def served_payload() -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)

        def log_message(self, *_args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/Carabiner_Mac.dmg"
    server.shutdown()
    server.server_close()


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_download_that_hashes_to_its_pin_is_accepted(served_payload: str, tmp_path: Path) -> None:
    dest = tmp_path / "Carabiner_Mac.dmg"
    fetch_verified_dmg(served_payload, PAYLOAD_SHA256, dest)
    assert dest.read_bytes() == PAYLOAD


def test_a_download_that_does_not_hash_to_its_pin_is_checksum_mismatch(served_payload: str, tmp_path: Path) -> None:
    wrong_pin = hashlib.sha256(PAYLOAD + b"x").hexdigest()
    with pytest.raises(CarabinerProvisionError) as refused:
        fetch_verified_dmg(served_payload, wrong_pin, tmp_path / "Carabiner_Mac.dmg")
    assert refused.value.code == "checksum_mismatch"
    assert PAYLOAD_SHA256 in str(refused.value) and wrong_pin in str(refused.value)
    assert refused.value.user_message.startswith("[carabiner_checksum_mismatch] ")


def test_verify_sha256_is_exact(tmp_path: Path) -> None:
    path = tmp_path / "Carabiner"
    path.write_bytes(PAYLOAD)
    verify_sha256(path, PAYLOAD_SHA256, "binary")  # positive control
    path.write_bytes(PAYLOAD[:-1])
    with pytest.raises(CarabinerProvisionError, match="not the pinned release"):
        verify_sha256(path, PAYLOAD_SHA256, "binary")


def test_an_unreachable_download_is_download_failed_naming_the_url(tmp_path: Path) -> None:
    url = f"http://127.0.0.1:{_closed_port()}/Carabiner_Mac.dmg"
    with pytest.raises(CarabinerProvisionError) as failed:
        fetch_verified_dmg(url, PAYLOAD_SHA256, tmp_path / "Carabiner_Mac.dmg")
    assert failed.value.code == "download_failed"
    assert url in str(failed.value) and "press LINK again" in str(failed.value)


def test_team_identifier_reads_the_exact_line() -> None:
    assert team_identifier(CODESIGN_DETAIL) == DEEP_SYMMETRY_TEAM_ID
    assert team_identifier(CODESIGN_DETAIL.replace(DEEP_SYMMETRY_TEAM_ID, f"{DEEP_SYMMETRY_TEAM_ID}X")) == f"{DEEP_SYMMETRY_TEAM_ID}X"
    assert team_identifier("Identifier=org.deepsymmetry.Carabiner\nSignature=adhoc\nTeamIdentifier=not set\n") == "not set"
    assert team_identifier("Identifier=x\n") is None


def test_signature_verdicts() -> None:
    assert signature_problem(0, "", CODESIGN_DETAIL, DEEP_SYMMETRY_TEAM_ID) is None  # positive control
    longer = CODESIGN_DETAIL.replace(DEEP_SYMMETRY_TEAM_ID, f"{DEEP_SYMMETRY_TEAM_ID}X")
    assert signature_problem(0, "", longer, DEEP_SYMMETRY_TEAM_ID) is not None, "a substring team match was accepted"
    other = CODESIGN_DETAIL.replace(DEEP_SYMMETRY_TEAM_ID, "ABCDE12345")
    assert "ABCDE12345" in (signature_problem(0, "", other, DEEP_SYMMETRY_TEAM_ID) or "")
    unsigned = signature_problem(1, "code object is not signed at all", CODESIGN_DETAIL, DEEP_SYMMETRY_TEAM_ID)
    assert unsigned is not None and "not signed at all" in unsigned


def test_install_refuses_any_host_but_macos(tmp_path: Path) -> None:
    with pytest.raises(CarabinerProvisionError) as refused:
        install_pinned_carabiner(tmp_path, platform="linux")
    assert refused.value.code == "unsupported_platform"
    assert not any(tmp_path.iterdir()), "a refused install wrote files"


def test_an_already_verified_install_is_reused_without_a_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotence: a binary that hashes to the pin is used as-is (no network, no hdiutil)."""
    binary = provider.installed_binary(tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(PAYLOAD)
    monkeypatch.setattr(provider, "PINNED_BINARY_SHA256", PAYLOAD_SHA256)  # the pin, not the outcome
    lines: list[str] = []
    assert install_pinned_carabiner(tmp_path, platform="darwin", log=lines.append) == binary
    assert lines and "already installed and verified" in lines[0]


def test_the_sidecar_cli_installs_through_the_engine_module() -> None:
    from scripts import carabiner_sidecar

    assert carabiner_sidecar.provider is provider
    assert carabiner_sidecar.installed_binary() == provider.installed_binary(provider.INSTALL_ROOT)
    assert carabiner_sidecar.CARABINER_VERSION == provider.CARABINER_VERSION
