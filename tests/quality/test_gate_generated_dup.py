"""Generated contract artifacts must not be scored for duplication.

`apps/webui/openapi.json` and `apps/webui/frontend/src/lib/api-types.ts` are
written by `just pre-push` and by CI, from the FastAPI app and from that dump.
Nobody edits them, so a clone jscpd finds inside them names no work anyone can
do -- and the two files repeat by construction, because every endpoint carries
the same 422 `HTTPValidationError` response block.

That is the same argument CFG.LOCKFILE_GLOBS already records for lockfiles,
and it stopped being theoretical on Sat 19 Sep 2026: main's committed
`openapi.json` had been truncated from 286 paths to 134 (commit 76dcf21ba),
which broke the contract-drift gate on every PR based on main. Restoring the
150 missing endpoints is the fix, and restoring them moved
`duplication.percent` from 0.31 to 0.39 -- so the gate would have blocked the
repair of its own trunk. Measured on the restored tree: 0.39 with the two
files scored, 0.21 without.

Regression lines:
  - if a generated contract artifact is scored for duplication then restoring a
    truncated contract file fails the quality gate, so broken
  - if a glob here names a file that no longer exists then the ignore list has
    gone stale and is silently protecting nothing, so broken
  - if the duplication evaluator stops passing these globs to jscpd then the
    ignore is dead config, so broken
"""

from __future__ import annotations

import inspect
from pathlib import Path

from scripts.quality_gate import CFG, _jscpd_duplication

REPO = Path(__file__).resolve().parents[2]


def test_generated_contract_globs_are_ignored_for_duplication() -> None:
    """[if] a generated contract artifact is scored for duplication [then broken]"""
    ignored = set(CFG.LOCKFILE_GLOBS) | set(CFG.GENERATED_CONTRACT_GLOBS)
    assert "**/webui/openapi.json" in ignored, (
        "if the generated OpenAPI dump is scored for duplication then a full, "
        "correct contract file fails the gate that exists to defend it - broken"
    )
    assert "**/src/lib/api-types.ts" in ignored, (
        "if the generated api-types.ts is scored for duplication then the same "
        "restoration fails twice - broken"
    )


def test_every_generated_glob_still_names_a_real_file() -> None:
    """[if] a glob names a file that no longer exists [then broken]"""
    for glob in CFG.GENERATED_CONTRACT_GLOBS:
        suffix = glob.removeprefix("**/")
        matches = [p for p in REPO.glob(f"apps/**/{suffix}") if p.is_file()]
        assert matches, (
            f"{glob} matches nothing under apps/, so it protects nothing and "
            "the next reader will believe it does - broken"
        )


def test_the_duplication_evaluator_passes_the_generated_globs_to_jscpd() -> None:
    """[if] the evaluator stops passing these globs [then broken]"""
    source = inspect.getsource(_jscpd_duplication)
    assert "GENERATED_CONTRACT_GLOBS" in source, (
        "if the evaluator does not hand these globs to jscpd then the ignore "
        "list is dead config and the files are scored anyway - broken"
    )
