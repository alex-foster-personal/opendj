"""Decide whether an empty PR files listing is a real zero-diff PR or a broken read.

Split out of ``scripts/pr_ci_coverage.py`` to keep that module under the 600-line
quality ratchet. Pure: the caller fetches the single-PR object and passes it in, so
this module does no I/O and the caller's ``_gh_api_json`` test seam is unchanged.

MINI-PRD
    R1 Zero-diff PRs ............................................. done + regression
       [if] a PR's files listing is empty and its own PR object reports
                 changed_files 0 [then] accept it as a zero-diff PR, which
                 ``_inspect_pr`` leaves out of the required-CI denominator with
                 no runs lookup [else stop] (PR #3771: a draft with one empty
                 commit made every ci-budget-watch run exit 10 from Tue 22 Sep 2026,
                 publishing no coverage status for ANY PR)
       [if] the listing is empty but changed_files is nonzero [then] raise a
            precondition error, since a truncated listing looks identical [else stop]
       [if] changed_files is absent, null, a string, a float or a bool
            [then] raise a precondition error [else stop]
       [if] the PR object's head.sha is not the inspected head [then] raise a
            precondition error, since a push between the reads looks identical [else stop]

-Claude
"""

from __future__ import annotations

from scripts.ci_health_core import PreconditionError


def require_corroborated_empty_diff(pr_number: int, head_sha: str, pull: object) -> None:
    """Accept an empty files listing only when the PR object independently says 0.

    Absence alone never passes: an empty listing is also what a truncated or broken
    response looks like, so the separate single-PR read must report exactly the
    integer 0. ``type(...) is int`` rather than ``isinstance`` because ``False == 0``.
    That read must also describe the INSPECTED head: a push between the two reads
    could otherwise let head B's zero diff excuse head A's broken empty listing.
    """
    body = pull if isinstance(pull, dict) else {}
    changed = body.get("changed_files")
    if type(changed) is not int or changed != 0:
        raise PreconditionError(
            f"PR #{pr_number} files listing is empty but the PR object reports "
            f"changed_files={changed!r}"
        )
    head = body.get("head")
    seen_head = head.get("sha") if isinstance(head, dict) else None
    if seen_head != head_sha:
        raise PreconditionError(
            f"PR #{pr_number} zero-diff PR object describes head {seen_head!r}, "
            f"not the inspected head {head_sha!r}"
        )
