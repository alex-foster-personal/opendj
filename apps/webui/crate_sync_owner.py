"""Owner-Mac SSH target, repo and key configuration for crate sync pulls.

Split out of :mod:`apps.webui.crate_sync` (issue #9687); ``crate_sync``
re-exports every name here.
"""

from __future__ import annotations

import os
from pathlib import Path

from apps.webui.crate_sync_model import OWNER_REPO_ENV, OWNER_SSH_KEY_ENV, OWNER_SSH_TARGETS_ENV

# ----- owner ssh / remote pull ---------------------------------------------


def _owner_ssh_targets() -> tuple[str, ...]:
    """This deployment's allowed owner-Mac SSH targets, read fresh from the
    environment (not cached at import time) so a test can set/unset the
    variable per case. Raises when empty: an empty allowlist must refuse
    every owner explicitly, not compare true against nothing.
    """
    raw = os.environ.get(OWNER_SSH_TARGETS_ENV, "")
    targets = tuple(t for t in raw.split(os.pathsep) if t)
    if not targets:
        raise RuntimeError(
            f"{OWNER_SSH_TARGETS_ENV} is not set -- export this deployment's "
            "owner-Mac SSH target(s) (user@host, os.pathsep-separated; the "
            "first is used as the --owner default) before a --remote pull"
        )
    return targets


def _owner_ssh_default() -> str:
    return _owner_ssh_targets()[0]


def _owner_repo_default() -> Path:
    """This deployment's owner-Mac repo checkout, read fresh from the
    environment. Raises when unset: a --remote pull with no --owner-repo and
    no configured default must fail loudly rather than trust a placeholder
    that matches nothing real (#910).
    """
    raw = os.environ.get(OWNER_REPO_ENV)
    if not raw:
        raise RuntimeError(
            f"{OWNER_REPO_ENV} is not set and --owner-repo was not given -- "
            "export this deployment's owner-Mac repo checkout path"
        )
    return Path(raw)


def allowed_owner_ssh(owner: str) -> bool:
    return owner in _owner_ssh_targets()


def owner_ssh_key_path() -> Path:
    """The configured owner-Mac pull key, or a hard failure.

    No default: this used to be a literal path under one deployment's /root, and
    defaulting an unset value to a path that exists nowhere is how a pull lane
    reports a missing key as a permission error three layers down.
    """
    raw = os.environ.get(OWNER_SSH_KEY_ENV, "").strip()
    if not raw:
        raise RuntimeError(
            f"{OWNER_SSH_KEY_ENV} must name the owner Mac SSH private key the "
            "crate pull lane authenticates with; unset is not defaulted"
        )
    return Path(raw)


def _require_owner_ssh_key() -> Path:
    """The configured key, proven to exist. Both pull lanes open with this."""
    key = owner_ssh_key_path()
    if not key.is_file():
        raise RuntimeError(f"owner SSH key missing: {key}")
    return key


def ssh_owner_argv(command: str, *, owner: str) -> list[str]:
    if not allowed_owner_ssh(owner):
        raise RuntimeError(f"refusing owner SSH target {owner!r}; allowed {sorted(_owner_ssh_targets())}")
    key = _require_owner_ssh_key()
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(key),
        owner,
        command,
    ]
