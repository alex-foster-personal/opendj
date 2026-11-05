"""LINKBUILD-01: each engine compares the partner's build and Link rules (FE-logic audit #6646 row 65).

Before this nothing compared peer builds: compatibility was the wire version "1" plus optional
fields, so two laptops could run different take-over or ON AIR rules and disagree in silence.

Each engine stamps its now-playing answer with ``build`` (``build_stamp``). The reading engine
compares the stamp with its own (``compare_builds``) and puts the verdict on its peer answer as
``peer_build``. Both engines read each other, so both laptops reach a verdict.

``LINK_RULES_VERSION`` is the number that matters: a commit difference alone is information (a
fix elsewhere in the app), a rules difference means the two engines may decide the same moment
differently, so the page warns.
"""

from __future__ import annotations

from typing import Any, Final, Literal

#: Bump when any rule both engines must apply identically changes: the take-over clash rule and
#: the timeline-jump decision (LINKROLE-01/03), peer liveness (LINKROLE-02), ON AIR (LINKAIR-01).
LINK_RULES_VERSION: Final[int] = 1

Verdict = Literal["same", "different_build", "different_rules", "unknown"]


def build_stamp(*, git_sha: str | None, app_version: str | None, identity_error: str | None) -> dict[str, Any]:
    """This engine's stamp. The rules version is code, always known; the commit may not be, and
    then ``identity_error`` says why."""
    if (git_sha is None) == (identity_error is None):
        raise ValueError(f"build_stamp: exactly one of git_sha and identity_error, got {git_sha!r}, {identity_error!r}")
    return {"link_rules": LINK_RULES_VERSION, "git_sha": git_sha, "app_version": app_version, "identity_error": identity_error}


def _usable_stamp(stamp: Any) -> dict[str, Any] | None:
    if not isinstance(stamp, dict):
        return None
    rules = stamp.get("link_rules")
    if isinstance(rules, bool) or not isinstance(rules, int):
        return None
    sha, app_version = stamp.get("git_sha"), stamp.get("app_version")
    return {"link_rules": rules, "git_sha": sha if isinstance(sha, str) else None,
            "app_version": app_version if isinstance(app_version, str) else None}


def compare_builds(ours: dict[str, Any], theirs: Any) -> dict[str, Any]:
    """The verdict on the partner's stamp. No usable stamp (an older build) is unknown, never same."""
    verdict: Verdict
    their_stamp = _usable_stamp(theirs)
    if their_stamp is None:
        verdict, message = "unknown", "The partner's build does not state its Link rules (an older build): update both laptops"
    elif their_stamp["link_rules"] != ours["link_rules"]:
        verdict = "different_rules"
        message = (f"Different Link rules: this laptop runs version {ours['link_rules']}, the partner runs "
                   f"version {their_stamp['link_rules']}, so take-over and ON AIR may disagree: update both laptops")
    elif ours["git_sha"] is not None and their_stamp["git_sha"] is not None and ours["git_sha"] != their_stamp["git_sha"]:
        verdict = "different_build"
        message = f"Different builds ({ours['git_sha']} here, {their_stamp['git_sha']} there), same Link rules"
    else:
        verdict, message = "same", "Same Link rules"
    return {"verdict": verdict, "message": message, "ours": ours, "theirs": their_stamp}
