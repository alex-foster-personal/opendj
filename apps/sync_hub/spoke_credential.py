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

import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from apps.sync_hub.machine_credentials import CREDENTIAL_PREFIX

log = logging.getLogger("apps.sync_hub.client")

CREDENTIAL_FILENAME: str = "sync-credential"
CREDENTIAL_FILE_MODE: int = 0o600
NOTICE_FILENAME: str = "state/cloudsync-credential-notice.json"
REENROLL_ACTION: str = (
    "Re-enroll to collect a sync credential (python -m apps.sync_hub enroll)"
)

#: In-process dedup for ``log_hub_verdict`` within one process lifetime.
_warned_verdicts: set[tuple[str, str]] = set()


class SpokeCredentialError(RuntimeError):
    """The credential file exists but cannot be trusted or used."""


@dataclass(frozen=True)
class CredentialNotice:
    verdict: str
    action: str
    hub_machine_id: str


def credential_path(data_dir: Path) -> Path:
    return Path(data_dir) / CREDENTIAL_FILENAME


def _notice_path(data_dir: Path) -> Path:
    return Path(data_dir) / NOTICE_FILENAME


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


def _persist_notice(data_dir: Path, hub_machine_id: str, verdict: str) -> None:
    path = _notice_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hub_machine_id": hub_machine_id,
        "verdict": verdict,
        "action": REENROLL_ACTION,
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(path)


def log_hub_verdict(hub_machine_id: str, verdict: object) -> None:
    """Warn when the hub's ``hello`` says it would not accept our credential.

    The spoke's half of the OBSERVE report: an operator reading this
    machine's sync log learns that ENFORCE would refuse it before ENFORCE is
    switched on. ``None`` is a hub built before v11, which reports nothing.
    """
    if verdict is None or verdict == "valid":
        return
    verdict_s = str(verdict)
    key = (hub_machine_id, verdict_s)
    if key in _warned_verdicts:
        log.debug(
            "hub %s still reads this machine's sync credential as %s",
            hub_machine_id,
            verdict_s,
        )
        return
    _warned_verdicts.add(key)
    log.warning(
        "hub %s reads this machine's sync credential as %s; it syncs today "
        "only because the hub runs in observe mode. %s.",
        hub_machine_id,
        verdict_s,
        REENROLL_ACTION,
    )


def record_credential_notice(data_dir: Path, hub_machine_id: str, verdict: object) -> None:
    """Persist one re-enroll notice for status until the verdict changes."""
    if verdict is None or verdict == "valid":
        return
    verdict_s = str(verdict)
    path = _notice_path(data_dir)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if (
            isinstance(existing, dict)
            and existing.get("hub_machine_id") == hub_machine_id
            and existing.get("verdict") == verdict_s
        ):
            return
    _persist_notice(data_dir, hub_machine_id, verdict_s)


def credential_notice(data_dir: Path) -> CredentialNotice | None:
    path = _notice_path(data_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    hub_machine_id = payload.get("hub_machine_id")
    verdict = payload.get("verdict")
    action = payload.get("action")
    if not isinstance(hub_machine_id, str) or not isinstance(verdict, str):
        return None
    if verdict not in ("missing", "invalid", "revoked", "unowned"):
        return None
    return CredentialNotice(
        verdict=verdict,
        action=str(action) if isinstance(action, str) else REENROLL_ACTION,
        hub_machine_id=hub_machine_id,
    )


__all__ = [
    "CREDENTIAL_FILENAME",
    "CREDENTIAL_FILE_MODE",
    "CredentialNotice",
    "REENROLL_ACTION",
    "SpokeCredentialError",
    "credential_notice",
    "credential_path",
    "log_hub_verdict",
    "read_credential",
    "record_credential_notice",
    "write_credential",
]
