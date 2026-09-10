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

THE FOURTH STATE (SAND-01).  A dead control in this app already carries one of
three sentences, and they mean different things::

    1. not built              "not implemented - see PARITY-TODO"
    2. this daemon does not offer it   capabilities.svelte.ts refusals
    3. not on your plan       apps.entitlements.UI_REFUSAL_TITLE

"The App Store build cannot do this" is a FOURTH fact and gets a fourth
sentence, :data:`STORE_BUILD_REFUSAL_TITLE`, for the same reason the third one
did: telling a user a feature is "not implemented" when the truth is "Apple's
sandbox forbids it" is a lie about why the control is dead.  The four are
pinned apart by
``apps/webui/frontend/tests/unit/store-build-refusal.test.mjs``.

WHAT A REFUSAL MUST NOT SAY.  It must not send the user to a download outside
the store.  App Store Review Guideline 3.2.2(vi) bars an app from requiring a
user to download something else to access functionality, and Apple reads
"get our other build to unlock this" as circumventing the store, so a helpful
sounding "use the direct download instead" in a tooltip is a rejection risk
shipped in a string.  Shipping a store build that simply does not have the
feature is fine; ADVERTISING the way around it from inside that build is not.
See section 3, option B of ``specs/appstore-sandbox-remediation.md``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Set by macOS inside a sandboxed process. The primary signal.
CONTAINER_ENV: str = "APP_SANDBOX_CONTAINER_ID"

#: Where macOS redirects a sandboxed process's home.
_CONTAINER_MARKER = "/Library/Containers/"

#: The stable code every store-build refusal carries, on the wire and in the
#: exception. Callers branch on this, never on the prose.
STORE_BUILD_REFUSAL_CODE: str = "capability_not_in_store_build"

#: The exact tooltip a control carries when the App Store build cannot offer
#: it. The FOURTH distinct sentence (see the module docstring): not the
#: PARITY-TODO wording, not a capability refusal, not the plan refusal. It
#: names the BUILD and the reason, and deliberately points nowhere outside
#: the store.
STORE_BUILD_REFUSAL_TITLE: str = (
    "not available in the App Store build - the macOS App Sandbox does not "
    "permit it"
)


class SandboxRefusal(RuntimeError):
    """A capability that cannot work inside the sandbox was asked for anyway.

    Raised rather than returning an empty result on purpose. The whole reason
    this module exists is that the sandbox's failure mode is silence, and a
    silent empty answer is indistinguishable from a true empty answer.
    """


def is_sandboxed(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> bool:
    """True when this process is inside an App Sandbox container.

    Two independent signals, because either alone can be defeated: the env var
    is absent in some spawn paths that still inherit the container, and a
    developer can point HOME at a container-shaped path without being
    sandboxed. Either being true is enough; the cost of a false positive is an
    explicit refusal, and the cost of a false negative is the silent empty
    answer this exists to prevent.

    ``platform`` is injectable for the same reason ``environ`` is: the App
    Sandbox is macOS-only, so on Linux this would always short-circuit to
    False and the signal logic would go untested on a Linux CI runner. Passing
    it keeps the decision a pure function of its inputs, testable anywhere.
    """
    env = os.environ if environ is None else environ
    host = sys.platform if platform is None else platform
    if host != "darwin":
        return False
    if env.get(CONTAINER_ENV):
        return True
    return _CONTAINER_MARKER in str(Path(env.get("HOME", "")))


def store_build_refusal_message(
    capability: str, *, because: str, instead: str | None = None
) -> str:
    """The human sentence a store-build refusal carries.

    Spelled once here so the exception a caller raises, the HTTP body a route
    returns and the tooltip a control renders cannot drift into three
    different explanations of the same fact -- the contract
    ``apps.entitlements`` holds for the plan refusal.

    ``because`` says what the sandbox blocks.  ``instead`` names a way forward
    THAT EXISTS INSIDE THIS BUILD, and is None when there is not one: a
    refusal with no alternative is a dead end, but inventing one that points
    at a download outside the store is worse than a dead end, because it is
    the App Store Review Guideline 3.2.2(vi) pattern (see the module
    docstring). Saying plainly that this build does not have the feature is
    the honest answer when it is the true one.
    """
    onward = f"{instead} " if instead else ""
    return (
        f"{capability} is not available in the App Store build of Open DJ. "
        f"{because} {onward}"
        "See specs/appstore-sandbox-remediation.md."
    )


def refuse_if_sandboxed(
    capability: str, *, because: str, instead: str | None = None
) -> None:
    """Raise :class:`SandboxRefusal` when sandboxed, saying why."""
    if not is_sandboxed():
        return
    raise SandboxRefusal(
        store_build_refusal_message(capability, because=because, instead=instead)
    )


__all__ = [
    "CONTAINER_ENV",
    "STORE_BUILD_REFUSAL_CODE",
    "STORE_BUILD_REFUSAL_TITLE",
    "SandboxRefusal",
    "is_sandboxed",
    "refuse_if_sandboxed",
    "store_build_refusal_message",
]
