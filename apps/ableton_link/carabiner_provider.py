"""Install the pinned Carabiner (Deep Symmetry, GPL-2.0) on first use: download, verify, install.

LINK-122 (ADR-0176-link-provider-and-local-network-permission, option (b2)): the packaged app has no
terminal and ships ``apps/`` only, so the engine itself fetches the pinned upstream release the
first time LINK is pressed and nothing answers on the Carabiner port. ``scripts/carabiner_sidecar.py``
(``just link-up``) is a thin CLI over the same functions, so the developer path and the packaged
path verify the binary identically.

Verification, every step a named failure (``CarabinerProvisionError.code``), never a fallback:

- ``download_failed``: the DMG could not be fetched (offline, HTTP error, timeout).
- ``checksum_mismatch``: the DMG or the extracted binary does not hash to its pin.
- ``signature_mismatch``: ``codesign --verify`` fails, or the TeamIdentifier line is not exactly
  Deep Symmetry's team (a substring match would accept ``9M6LKU948YX``).
- ``install_failed``: ``hdiutil`` could not mount the DMG, or the copy failed.
- ``unsupported_platform``: only the macOS release is pinned.

Licensing: Carabiner is GPL-2.0. This downloads upstream's own signed, notarized binary from
upstream's GitHub release. Open DJ does not bundle, modify or redistribute it, and talks to it only
over a localhost TCP socket (ADR link-tempo).

Stdlib only, on purpose: ``just link-up`` runs ``uv run --no-project python -m
scripts.carabiner_sidecar`` before any dependency is installed, so httpx (an engine dependency) is
not importable on that path.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from apps.shared.hashing import sha256_file_hex as sha256_of

# ----------------------------------------------------------------------------- config
# Pinned Sat 10 Oct 2026 05:35Z from the upstream release page. Both hashes were computed on
# silver from a fresh download; `spctl --assess --type install` said "accepted, source=Notarized
# Developer ID, origin=Developer ID Application: Deep Symmetry, LLC (9M6LKU948Y)".
CARABINER_VERSION: str = "1.2.0"
CARABINER_DMG_URL: str = "https://github.com/Deep-Symmetry/carabiner/releases/download/v1.2.0/Carabiner_Mac.dmg"
PINNED_DMG_SHA256: str = "13a53b16fb044fbfde46122804948e6d95137cc0a7812c3e7b34767817b5bdae"
#: The binary inside the DMG is universal (x86_64 + arm64), so one pin covers both Mac arches.
PINNED_BINARY_SHA256: str = "27d771f51d0f7cc6c9c51a65a44abbdfc6d15e6536840cc8b4793f1dd6602fca"
DEEP_SYMMETRY_TEAM_ID: str = "9M6LKU948Y"

INSTALL_ROOT: Path = Path.home() / ".local" / "opendj" / "carabiner"
#: The whole DMG download must finish within this; a stalled mirror is a named failure, not a hang.
DOWNLOAD_TIMEOUT_S: float = 60.0

ProvisionCode = Literal[
    "download_failed",
    "checksum_mismatch",
    "signature_mismatch",
    "install_failed",
    "unsupported_platform",
    "sandboxed",
    "port_held",
    "spawn_failed",
    "not_answering",
]

_TEAM_LINE = re.compile(r"^TeamIdentifier=(?P<team>[^\n]*?)[ \t]*$", re.MULTILINE)
_LOG = logging.getLogger(__name__)


class CarabinerProvisionError(RuntimeError):
    """Carabiner could not be installed or started. ``code`` is stable; the message is for a DJ."""

    def __init__(self, code: ProvisionCode, message: str) -> None:
        super().__init__(message)
        self.code: ProvisionCode = code

    @property
    def user_message(self) -> str:
        """``[code] message``: what the route returns and the LINK panel shows."""
        return f"[carabiner_{self.code}] {self}"


# ----------------------------------------------------------------------------- verdicts (pure)
def verify_sha256(path: Path, pinned: str, what: str) -> None:
    """Raise ``checksum_mismatch`` unless ``path`` hashes to ``pinned``."""
    actual = sha256_of(path)
    if actual != pinned:
        raise CarabinerProvisionError(
            "checksum_mismatch",
            f"the downloaded Carabiner {what} is not the pinned release (sha256 {actual}, pinned {pinned}). "
            "Nothing was installed.",
        )


def team_identifier(codesign_detail: str) -> str | None:
    """The ``TeamIdentifier=`` value from ``codesign -dv`` output, or None when there is none."""
    match = _TEAM_LINE.search(codesign_detail)
    return None if match is None else match.group("team")


def signature_problem(verify_returncode: int, verify_stderr: str, codesign_detail: str, team: str) -> str | None:
    """None when the signature is valid AND names exactly ``team``; otherwise why not."""
    if verify_returncode != 0:
        return f"codesign verification failed: {verify_stderr.strip()}"
    found = team_identifier(codesign_detail)
    if found != team:
        return f"signed by team {found!r}, not Deep Symmetry ({team})"
    return None


# ----------------------------------------------------------------------------- install
def installed_binary(root: Path = INSTALL_ROOT) -> Path:
    return root / CARABINER_VERSION / "Carabiner"


def download(url: str, dest: Path, timeout_s: float = DOWNLOAD_TIMEOUT_S) -> None:
    """Fetch ``url`` to ``dest``; ``download_failed`` with the reason on any network error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response, dest.open("wb") as out:
            shutil.copyfileobj(response, out)
    except OSError as exc:  # URLError, HTTPError and timeouts are all OSError
        raise CarabinerProvisionError(
            "download_failed",
            f"Open DJ could not download Carabiner, the Ableton Link helper, from {url} ({exc}). "
            "Connect to the internet once and press LINK again; after that it works offline.",
        ) from exc


def fetch_verified_dmg(url: str, pinned_sha256: str, dest: Path) -> None:
    download(url, dest)
    verify_sha256(dest, pinned_sha256, "DMG")


def verify_developer_id(binary: Path, team: str = DEEP_SYMMETRY_TEAM_ID) -> None:
    verify = subprocess.run(
        ["codesign", "--verify", "--strict", "--verbose=2", str(binary)], capture_output=True, text=True, check=False
    )
    detail = subprocess.run(["codesign", "-dv", str(binary)], capture_output=True, text=True, check=False)
    problem = signature_problem(verify.returncode, verify.stderr, detail.stderr, team)
    if problem is not None:
        raise CarabinerProvisionError("signature_mismatch", f"{binary} is not Deep Symmetry's Carabiner: {problem}")


def _extract_binary(dmg: Path, scratch: Path) -> Path:
    mountpoint = scratch / "mnt"
    attach = subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mountpoint), str(dmg)],
        capture_output=True,
        check=False,
        text=True,
    )
    if attach.returncode != 0:
        raise CarabinerProvisionError("install_failed", f"hdiutil attach failed: {attach.stderr.strip()}")
    try:
        staged = scratch / "Carabiner"
        shutil.copy2(mountpoint / "Carabiner", staged)
    except OSError as exc:
        raise CarabinerProvisionError("install_failed", f"could not copy Carabiner out of the DMG: {exc}") from exc
    finally:
        subprocess.run(["hdiutil", "detach", str(mountpoint)], capture_output=True, check=False)
    return staged


def install_pinned_carabiner(
    root: Path = INSTALL_ROOT, platform: str | None = None, log: Callable[[str], None] = _LOG.info
) -> Path:
    """Download, verify and install the pinned release; idempotent when already verified."""
    host = sys.platform if platform is None else platform
    if host != "darwin":
        raise CarabinerProvisionError(
            "unsupported_platform", f"only the macOS Carabiner is pinned (this is {host}); see FRIEND-SETUP.md"
        )
    binary = installed_binary(root)
    if binary.exists() and sha256_of(binary) == PINNED_BINARY_SHA256:
        log(f"[OK] Carabiner {CARABINER_VERSION} already installed and verified at {binary}")
        return binary
    with tempfile.TemporaryDirectory(prefix="opendj-carabiner-") as scratch_dir:
        scratch = Path(scratch_dir)
        dmg = scratch / "Carabiner_Mac.dmg"
        log(f"[..] downloading {CARABINER_DMG_URL}")
        fetch_verified_dmg(CARABINER_DMG_URL, PINNED_DMG_SHA256, dmg)
        log(f"[OK] DMG sha256 matches pin {PINNED_DMG_SHA256[:16]}...")
        staged = _extract_binary(dmg, scratch)
        verify_sha256(staged, PINNED_BINARY_SHA256, "binary")
        verify_developer_id(staged)
        binary.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), binary)
        binary.chmod(0o755)
    (root / "SOURCE.txt").write_text(
        f"Carabiner {CARABINER_VERSION} (GPL-2.0, Deep Symmetry LLC)\n"
        f"Downloaded from upstream at the user's request: {CARABINER_DMG_URL}\n"
        f"Source code: https://github.com/Deep-Symmetry/carabiner/tree/v{CARABINER_VERSION}\n"
        "Open DJ does not bundle or redistribute this binary.\n"
    )
    log(f"[OK] installed Carabiner {CARABINER_VERSION} to {binary} (binary sha256 + Developer ID verified)")
    return binary


__all__ = [
    "CARABINER_DMG_URL",
    "CARABINER_VERSION",
    "DEEP_SYMMETRY_TEAM_ID",
    "INSTALL_ROOT",
    "PINNED_BINARY_SHA256",
    "PINNED_DMG_SHA256",
    "CarabinerProvisionError",
    "ProvisionCode",
    "download",
    "fetch_verified_dmg",
    "install_pinned_carabiner",
    "installed_binary",
    "sha256_of",
    "signature_problem",
    "team_identifier",
    "verify_developer_id",
    "verify_sha256",
]
