"""Is this process running inside the macOS App Sandbox?

WHY THIS IS A RUNTIME QUESTION, NOT A BUILD FLAG (SAND-04).  A build constant
saying "this is the store build" can be wrong: it can be set on a dev run, or
missing on a store run because someone packaged from the wrong lane.  The
container is a FACT about the process, so ask the process.

WHY IT MATTERS.  A sandboxed process cannot read ``~/Library/Pioneer``, cannot
LIST ``/Volumes``, and gets an ``Application Support`` path silently
redirected into its container.  The last two are the dangerous ones: they do
not raise, they return nothing, which reads to a user as "no drives attached"
and "empty library" rather than "this build cannot see those".  Every caller
that would otherwise degrade silently asks here first and refuses loudly
instead.

The engine runs as a CHILD of the Tauri shell and is signed with
``com.apple.security.inherit``, so it is inside the same container and this
answers the same for both processes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Set by macOS inside a sandboxed process. The primary signal.
CONTAINER_ENV: str = "APP_SANDBOX_CONTAINER_ID"

#: Where macOS redirects a sandboxed process's home.
_CONTAINER_MARKER = "/Library/Containers/"


class SandboxRefusal(RuntimeError):
    """A capability that cannot work inside the sandbox was asked for anyway.

    Raised rather than returning an empty result on purpose. The whole reason
    this module exists is that the sandbox's failure mode is silence, and a
    silent empty answer is indistinguishable from a true empty answer.
    """


def is_sandboxed(environ: dict[str, str] | None = None) -> bool:
    """True when this process is inside an App Sandbox container.

    Two independent signals, because either alone can be defeated: the env var
    is absent in some spawn paths that still inherit the container, and a
    developer can point HOME at a container-shaped path without being
    sandboxed. Either being true is enough; the cost of a false positive is an
    explicit refusal, and the cost of a false negative is the silent empty
    answer this exists to prevent.
    """
    env = os.environ if environ is None else environ
    if sys.platform != "darwin":
        return False
    if env.get(CONTAINER_ENV):
        return True
    return _CONTAINER_MARKER in str(Path(env.get("HOME", "")))


def refuse_if_sandboxed(capability: str, *, because: str, instead: str) -> None:
    """Raise :class:`SandboxRefusal` when sandboxed, naming a way forward.

    ``because`` says what the sandbox blocks and ``instead`` says what the user
    or caller can actually do, because a refusal with no alternative is just a
    dead end with better wording.
    """
    if not is_sandboxed():
        return
    raise SandboxRefusal(
        f"{capability} is not available in the App Store build of Open DJ. "
        f"{because} {instead} "
        "See specs/appstore-sandbox-remediation.md."
    )


__all__ = [
    "CONTAINER_ENV",
    "SandboxRefusal",
    "is_sandboxed",
    "refuse_if_sandboxed",
]
