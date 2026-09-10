"""What makes a GTZAN scoring run publishable as a benchmark round.

SPLIT OUT OF `score_gtzan.py` because that file reached the repository's
600-line Python ceiling, and this is the coherent half: everything here decides
whether a set of figures may be called a PASS, and none of it computes a
figure. `tests/beatbench/test_score_gtzan_refusals.py` drives it directly.

Every rule below exists because the previous version printed the figures and
exited 0 anyway. The shape they share is the one the repository's verification
rule names: a check that cannot fail for the reason it exists is not a check.
"""

from __future__ import annotations

import argparse
from typing import Any

#: The clips GTZAN itself cannot supply. `jazz.00054` is truncated in the
#: published corpus and has failed in every decoder since it was first
#: distributed; it is reported in the artifact rather than scored as zero.
#: Anything else that errors is the CANDIDATE failing, and that is fatal:
#: excluding a crash from the denominator rewards crashing over answering
#: badly (Codex P1 BLOCKING, PR #1660, discussion_r3976348810).
KNOWN_UNANALYZABLE_FIXTURES = frozenset({"jazz_00054"})

#: The only non-digest value `--expect-fixtures-digest` accepts, for the ONE
#: run that has no earlier digest to be compared against.
ESTABLISH_DIGEST = "establish"


def fixtures_digest_argument(raw: str) -> str:
    """A fixtures digest, or the explicit request to mint one.

    The flag used to default to the empty string, so OMITTING it disabled the
    gate silently and the committed reproduction command did exactly that. A
    truncated pairs manifest, a different GTZAN extraction, or rows carrying no
    `decode_fingerprint` were then scored, given a freshly computed digest, and
    exited 0 (Codex P1 BLOCKING, PR #1660, discussion_r3976483262).

    It is REQUIRED now, and the first round over a new corpus says
    `--expect-fixtures-digest establish` out loud rather than by omission. The
    difference is that establishing is a decision in the command line, where a
    reviewer can see it, instead of an absence nobody can see.
    """
    if raw == ESTABLISH_DIGEST:
        return raw
    if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
        raise argparse.ArgumentTypeError(
            f"expected a 64-character lowercase hex sha256 or {ESTABLISH_DIGEST!r}, "
            f"not {raw!r}. An unparseable digest must not read as no digest."
        )
    return raw


def refusals(summary: dict[str, Any], unscorable: dict[str, list[str]]) -> list[str]:
    """Every reason this run may not be called a pass, in the order found."""
    # Two ways this command must NOT exit 0, both of which used to.
    #
    # A MISSING CLIP is not a smaller denominator. A truncated runner output or
    # an omitted shard put every absent clip in `no_result` and carried on, so a
    # benchmark over an arbitrary subset -- potentially the subset that excludes
    # the candidate's worst clips -- was accepted as a successful run (Codex P1
    # BLOCKING, discussion_r3975279137). A runner ERROR is different in kind:
    # GTZAN ships a documented corrupt file, so that category is expected and
    # is reported in the artifact rather than made fatal.
    #
    # An UNMEASURED control is not a passed control. The repo-scorer
    # cross-check is the reason this script exists beside `mir_eval` at all;
    # recording UNMEASURED and exiting 0 let automation accept a benchmark whose
    # stated control never ran (Codex P1 BLOCKING, discussion_r3975199353).
    problems: list[str] = []
    unexpected_errors = sorted(
        set(unscorable["runner_error"]) - KNOWN_UNANALYZABLE_FIXTURES
    )
    if unexpected_errors:
        problems.append(
            f"{len(unexpected_errors)} clips failed in the analyzer and are not "
            f"the known-unanalyzable fixture: {unexpected_errors[:5]}. Excluding "
            "them would let a candidate that crashes on its worst clips report a "
            "higher score than one that answers badly, which is the abstention "
            f"loophole the empty-estimate rule closes one door down. "
            f"{sorted(KNOWN_UNANALYZABLE_FIXTURES)} is the only allowlisted "
            "failure and it is GTZAN's own corrupt file."
        )
    if unscorable["no_result"]:
        problems.append(
            f"{len(unscorable['no_result'])} requested clips have no result at all "
            f"(e.g. {unscorable['no_result'][:5]}). A shard is missing or an output "
            "was truncated; scoring the subset would answer a different question."
        )
    if summary.get("our_scorer_f_mean") is None:
        problems.append(
            "the repo-scorer cross-check is UNMEASURED: "
            f"{summary.get('our_scorer_note')}. PYTHONPATH must point at the "
            "repository root. A declined control is not a passed one."
        )
    else:
        # The CONTINUITY half of the same control, which the F-measure check
        # cannot stand in for. F stays measurable at zero when every estimate
        # is empty, while CMLt and AMLt decline on every clip, so the previous
        # refusal passed a run in which two of the three cross-checks printed
        # UNMEASURED and neither had run (Codex P1 BLOCKING, PR #1660,
        # discussion_r3975376358). Checked only when the F cross-check ran at
        # all, so an unimportable `apps.analysis_bench` is reported once as its
        # own cause rather than three times as three symptoms.
        problems.extend(
            f"the repo-scorer {name.upper()} cross-check is UNMEASURED: "
            f"{summary.get(f'our_{name}_note')} No clip produced a value on "
            "both sides, so the continuity metric was never compared. A "
            "control that declined is not a control that passed."
            for name in ("cmlt", "amlt")
            if not summary.get(f"our_{name}_n_comparable")
        )
    return problems
