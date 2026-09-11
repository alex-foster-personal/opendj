"""The spoke's copy of its sync credential: ``<data-dir>/sync-credential``, 0600.

The hub returns the raw credential exactly once, in the ``/enroll`` response
that minted it (:mod:`apps.sync_hub.machine_credentials`). This module is the
only place the spoke writes or reads it, and the one rule it holds is the one
``ssh`` holds for a private key: a credential file other local users can read
is refused rather than used, because a bearer that leaked to another account
on the box is indistinguishable from the real machine forever after.

The file lives beside ``machine-id`` in the data dir, not in the state DB: a
state DB copied to another machine (a restore, a debugging copy) must not
carry this machine's bearer with it, for the same reason ``machine-id`` sits
outside the DB (ADR 05 section 1).
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path

from apps.sync_hub.machine_credentials import CREDENTIAL_PREFIX

log = logging.getLogger("apps.sync_hub.client")

CREDENTIAL_FILENAME: str = "sync-credential"
CREDENTIAL_FILE_MODE: int = 0o600


class SpokeCredentialError(RuntimeError):
    """The credential file exists but cannot be trusted or used."""


def credential_path(data_dir: Path) -> Path:
    return Path(data_dir) / CREDENTIAL_FILENAME


def write_credential(data_dir: Path, token: str) -> Path:
    """Persist ``token`` at 0600, atomically replacing any previous one.

    Written to a temp file that is born 0600 (``mkstemp``) and renamed into
    place, so there is no instant at which the file exists with wider
    permissions or half-written.
    """
    if not token.startswith(CREDENTIAL_PREFIX):
        raise SpokeCredentialError(
            f"refusing to store a sync credential without the "
            f"{CREDENTIAL_PREFIX!r} prefix; the hub did not mint it."
        )
    target = credential_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".sync-credential.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, CREDENTIAL_FILE_MODE)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target


def read_credential(data_dir: Path) -> str | None:
    """This machine's credential, or None when it has never been issued one.

    None is a real state, not a failure: every machine enrolled before v11
    has no file, and an OBSERVE hub accepts it and reports ``missing``. A file
    that exists but is readable by group or others, empty, or not a minted
    credential raises instead of being sent or skipped. Windows has no POSIX
    mode bits to check and is not a first-class CloudSync client in v1
    (ADR 7), so the mode check is POSIX-only.
    """
    path = credential_path(data_dir)
    if not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if os.name != "nt" and mode & 0o077:
        raise SpokeCredentialError(
            f"{path} is mode {mode:04o}; a sync credential must be readable by "
            f"its owner only. Run `chmod 600 {path}`, or re-enroll with a fresh "
            f"grant if another account may have read it."
        )
    token = path.read_text(encoding="utf-8").strip()
    if not token.startswith(CREDENTIAL_PREFIX):
        raise SpokeCredentialError(
            f"{path} does not hold a sync credential (expected the "
            f"{CREDENTIAL_PREFIX!r} prefix); re-enroll with a fresh grant."
        )
    return token


def log_hub_verdict(hub_machine_id: str, verdict: object) -> None:
    """Warn when the hub's ``hello`` says it would not accept our credential.

    The spoke's half of the OBSERVE report: an operator reading this
    machine's sync log learns that ENFORCE would refuse it before ENFORCE is
    switched on. ``None`` is a hub built before v11, which reports nothing.
    """
    if verdict is None or verdict == "valid":
        return
    log.warning(
        "hub %s reads this machine's sync credential as %s; it syncs today "
        "only because the hub runs in observe mode. Re-enroll with a fresh "
        "grant (python -m apps.sync_hub enroll) to collect a credential.",
        hub_machine_id,
        verdict,
    )


__all__ = [
    "CREDENTIAL_FILENAME",
    "CREDENTIAL_FILE_MODE",
    "SpokeCredentialError",
    "credential_path",
    "log_hub_verdict",
    "read_credential",
    "write_credential",
]
