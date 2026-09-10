"""The claude-review lane's argv must be argv the INSTALLED cli accepts.

Why this file exists: ``scripts/claude_review.py`` passed
``--permission-prompts none`` long after the Claude Code CLI dropped that
option. The lane failed closed -- exit 3, ``unknown option`` -- which is the
right failure, but nothing exercised the argv, so it was discovered only when
a PR needed the third reviewer and found the lane dead under merge pressure
(PR #1648, Thu 10 Sep 2026).

The check is an INVARIANT, not a value: it does not restate the flag list, it
asks the installed binary whether it knows every flag the lane emits. A flag
renamed upstream fails here, and a flag ADDED to the lane is covered without
anybody remembering to update a list.

[if] the lane emits a flag the cli lacks [then] review is dead, [else stop].

Regression one-liners:
  - if the lane emits a flag the installed claude cli rejects then broken
  - if the probe reports clean while no cli is installed then broken
  - if the probe reports an invented flag as known then broken
"""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from scripts import claude_review

pytestmark = pytest.mark.requirement("CAT-04")

#: Every long option the cli documents, extracted from its own --help.
_FLAG_RE = re.compile(r"--[a-zA-Z][a-zA-Z0-9-]*")


def _cli() -> str:
    path = shutil.which("claude")
    if path is None:
        pytest.skip(
            "UNAVAILABLE: no `claude` on PATH, so this machine cannot say "
            "whether the lane's flags are accepted. Reporting a pass here "
            "would be the tool answering a question it did not measure."
        )
    return path


def _known_flags(cli: str) -> set[str]:
    """Every long option the installed cli documents in its own --help.

    Read from --help rather than by FEEDING each flag to the binary. The
    obvious probe -- run the cli with the flag and look for "unknown option"
    -- was written first and its control caught it immediately: `--help`
    short-circuits before the parser objects, so EVERY flag looked accepted,
    including a deliberately bogus one. The forms that do reach the parser
    (`--print`) also reach a model call, which a unit test must not make.
    """
    done = subprocess.run([cli, "--help"], capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise RuntimeError(
            f"`{cli} --help` exited {done.returncode}; this check cannot "
            f"measure anything from that. stderr: {done.stderr[:400]}"
        )
    return set(_FLAG_RE.findall(done.stdout + done.stderr))


def test_the_probe_does_not_report_an_invented_flag_as_known() -> None:
    """[if] an invented flag reads as known [then] all below is vacuous, [else stop].

    The NEGATIVE control, and it has already earned its keep.

    The NEGATIVE control, and it has already earned its keep: the first
    version of this module fed each flag to the cli alongside `--help` and
    looked for "unknown option". `--help` short-circuits before the parser
    complains, so the real check passed while measuring nothing, and only
    this control said so.
    """
    assert "--this-flag-will-never-exist" not in _known_flags(_cli()), (
        "an obviously invented flag reads as known, so this module's "
        "extraction is matching something other than the cli's option list "
        "and its verdicts mean nothing"
    )


def test_every_flag_the_review_lane_emits_is_accepted() -> None:
    """[if] the cli lacks a flag the lane emits [then] review is dead, [else stop].

    The third reviewer being dead is not otherwise visible until a merge
    needs it, which is how `--permission-prompts` survived a CLI rename.
    """
    argv = claude_review._claude_argv("claude-opus-5", "high")
    assert argv[0] == "claude", (
        f"control: the lane no longer invokes `claude` ({argv[0]!r}), so the "
        "flags below are being checked against the wrong binary"
    )
    flags = [token for token in argv if token.startswith("--")]
    assert flags, "control: no flags were extracted, so nothing was checked"

    known = _known_flags(_cli())
    assert known, "control: --help yielded no flags at all, so nothing was read"

    unknown = sorted(set(flags) - known)
    assert not unknown, (
        f"the installed claude cli documents no {unknown}. The review lane "
        "will fail closed with exit 3 the next time a PR needs it. Update "
        "scripts/claude_review.py._claude_argv."
    )
