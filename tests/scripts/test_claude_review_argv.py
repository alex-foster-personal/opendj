"""The claude-review lane's argv must be argv the INSTALLED cli accepts.

Why this file exists: ``scripts/claude_review.py`` passed
``--permission-prompts none`` long after the Claude Code CLI dropped that
option. The lane failed closed -- exit 3, ``unknown option`` -- which is the
right failure, but nothing exercised the argv, so it was discovered only when
a PR needed the third reviewer and found the lane dead under merge pressure
(PR #1648, Thu 10 Sep 2026).

The check is an INVARIANT, not a value: it does not restate the flag list, it
asks the installed binary whether it knows every flag AND every enumerated
value the lane emits. A flag renamed upstream fails here, a value dropped
from an enum fails here, and a flag ADDED to the lane is covered without
anybody remembering to update a list.

Flag names alone are not enough: ``--help`` short-circuits before the parser
validates a VALUE, so a probe that only confirms ``--permission-mode`` exists
would stay green even if ``dontAsk`` stopped being one of its choices --
which is exactly the ``unknown option`` failure mode this file exists to
catch, just moved from the flag to its argument. ``_flag_value_pairs`` below
extracts ``(flag, value)`` and checks the value against the choices the
installed cli documents for that flag, not just the flag's presence.

[if] the lane emits a flag or value the cli lacks [then] review is dead, [else stop].

Regression one-liners:
  - if the lane emits a flag the installed claude cli rejects then broken
  - if the lane emits an enum value the cli does not document for that flag then broken
  - if the probe reports clean while no cli is installed then broken
  - if the probe reports an invented flag as known then broken
  - if the probe reads a flag mentioned only in another option's help prose as known then broken
  - if the installed cli no longer documents dontAsk as deny-by-default then broken
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import claude_review

pytestmark = pytest.mark.requirement("REVIEW-03")

#: A real option-column line: exactly two leading spaces, an optional short
#: alias, then a long flag (and optionally a second long alias), then an
#: optional value placeholder. Anchoring on the exact two-space indent is
#: what excludes a flag named only in another option's WRAPPED description
#: text, which commander.js indents to the far wider description column.
_OPTION_LINE_RE = re.compile(
    r"^  (?:-\w, )?--[a-zA-Z][a-zA-Z0-9-]*(?:,\s+--[a-zA-Z][a-zA-Z0-9-]*)?"
    r"(?:\s+(?:<[^>]+>|\[[^\]]+\]))?",
    re.MULTILINE,
)
_LONG_FLAG_RE = re.compile(r"--[a-zA-Z][a-zA-Z0-9-]*")
_PLACEHOLDER_RE = re.compile(r"<[^>]+>|\[[^\]]+\]")
_CHOICES_RE = re.compile(r"\(choices:\s*(.*?)\)", re.DOTALL)
_CHOICE_VALUE_RE = re.compile(r'"([^"]*)"')


@dataclass(frozen=True)
class _OptionSpec:
    takes_value: bool
    choices: set[str] | None


def _cli() -> str:
    path = shutil.which("claude")
    if path is None:
        pytest.skip(
            "UNAVAILABLE: no `claude` on PATH, so this machine cannot say "
            "whether the lane's flags are accepted. Reporting a pass here "
            "would be the tool answering a question it did not measure."
        )
    return path


def _help_text(cli: str) -> str:
    done = subprocess.run([cli, "--help"], capture_output=True, text=True, timeout=120, check=False)
    if done.returncode != 0:
        raise RuntimeError(
            f"`{cli} --help` exited {done.returncode}; this check cannot "
            f"measure anything from that. stderr: {done.stderr[:400]}"
        )
    return done.stdout + done.stderr


def _help_options(help_text: str) -> dict[str, _OptionSpec]:
    """Map every long flag in --help's OPTION COLUMN to whether it takes a
    value and, when the cli documents one, its enumerated choices.

    Read from --help rather than by FEEDING each flag to the binary. The
    obvious probe -- run the cli with the flag and look for "unknown option"
    -- was written first and its control caught it immediately: `--help`
    short-circuits before the parser objects, so EVERY flag looked accepted,
    including a deliberately bogus one. The forms that do reach the parser
    (`--print`) also reach a model call, which a unit test must not make.

    Parsing is anchored to the option column (see `_OPTION_LINE_RE`), not
    the whole help text: a naive `--[\\w-]+` scan over the full text also
    matches a flag named only in another option's wrapped description prose
    (e.g. "only works with --print and --output-format=stream-json" inside
    `--include-partial-messages`'s help), which would read a removed flag as
    still known and leave the lane dead with this probe green.
    """
    line_matches = list(_OPTION_LINE_RE.finditer(help_text))
    options: dict[str, _OptionSpec] = {}
    for i, m in enumerate(line_matches):
        end = line_matches[i + 1].start() if i + 1 < len(line_matches) else len(help_text)
        block = help_text[m.start() : end]
        choices_match = _CHOICES_RE.search(block)
        choices = set(_CHOICE_VALUE_RE.findall(choices_match.group(1))) if choices_match else None
        spec = _OptionSpec(
            takes_value=bool(_PLACEHOLDER_RE.search(m.group(0))),
            choices=choices,
        )
        for name in _LONG_FLAG_RE.findall(m.group(0)):
            options[name] = spec
    return options


def _known_flags(cli: str) -> set[str]:
    return set(_help_options(_help_text(cli)).keys())


def _flag_value_pairs(
    argv: list[str], options: dict[str, _OptionSpec]
) -> list[tuple[str, str | None]]:
    """Pair each `--flag` in argv with its value, per which flags --help
    itself says take one. A boolean flag (no placeholder in its --help
    entry, e.g. `--restricted`) is never treated as consuming the next argv
    token as a value.
    """
    pairs: list[tuple[str, str | None]] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("--"):
            spec = options.get(token)
            if spec is not None and spec.takes_value and i + 1 < len(argv):
                pairs.append((token, argv[i + 1]))
                i += 2
                continue
            pairs.append((token, None))
        i += 1
    return pairs


def test_the_probe_does_not_report_an_invented_flag_as_known() -> None:
    """[if] an invented flag reads as known [then] all below is vacuous, [else stop].

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


def test_every_flag_and_value_the_review_lane_emits_is_accepted() -> None:
    """[if] the cli lacks a flag or a documented value the lane emits [then]
    review is dead, [else stop].

    The third reviewer being dead is not otherwise visible until a merge
    needs it, which is how `--permission-prompts` survived a CLI rename.
    `--permission-mode` takes a fixed enum: if `dontAsk` stopped being a
    member, the cli would still exit non-zero on an invalid argument -- the
    same lane-dead failure -- while a flag-names-only probe stayed green.
    """
    argv = claude_review._claude_argv("claude-opus-5", "high")
    assert argv[0] == "claude", (
        f"control: the lane no longer invokes `claude` ({argv[0]!r}), so the "
        "flags below are being checked against the wrong binary"
    )
    flags = [token for token in argv if token.startswith("--")]
    assert flags, "control: no flags were extracted, so nothing was checked"

    options = _help_options(_help_text(_cli()))
    assert options, "control: --help yielded no options at all, so nothing was read"

    unknown = sorted(set(flags) - set(options))
    assert not unknown, (
        f"the installed claude cli documents no {unknown}. The review lane "
        "will fail closed with exit 3 the next time a PR needs it. Update "
        "scripts/claude_review.py._claude_argv."
    )

    checked_enum_values = False
    for flag, value in _flag_value_pairs(argv, options):
        choices = options[flag].choices
        if choices is None or value is None:
            continue
        checked_enum_values = True
        assert value in choices, (
            f"{flag} {value!r} is not among the installed cli's documented "
            f"choices {sorted(choices)}. The review lane will fail closed "
            "with exit 3 the next time a PR needs it. Update "
            "scripts/claude_review.py._claude_argv."
        )
    assert checked_enum_values, (
        "control: no (flag, value) pair in the lane's argv matched an "
        "enumerated option, so the value-validation loop above never ran "
        "and this test only checked flag names"
    )

    # The loop above is satisfied as soon as ANY flag's value gets checked,
    # e.g. --output-format alone keeps it green while --permission-mode goes
    # unchecked if the cli ever stops publishing its choices in the
    # `(choices: "a", "b")` form this probe parses (--effort already uses a
    # bare `(a, b)` form on this cli, so the alternate format is live in the
    # same help text). Pin the safety-critical flag directly.
    permission_mode = options.get("--permission-mode")
    assert permission_mode is not None and permission_mode.choices is not None, (
        "control: --permission-mode no longer publishes a parseable "
        "'(choices: ...)' list, so the generic loop above skipped it and "
        "dontAsk went unchecked. Update _CHOICES_RE or _help_options to "
        "parse the new format."
    )
    assert "dontAsk" in permission_mode.choices, (
        f"the installed cli's --permission-mode no longer documents "
        f"'dontAsk' among {sorted(permission_mode.choices)}. The review "
        "lane will fail closed with exit 3 the next time a PR needs it."
    )


def test_permission_mode_dontask_denies_rather_than_auto_approves() -> None:
    """[if] dontAsk auto-approves instead of denying [then] a hung or
    misbehaving review lane can silently approve a tool call nobody
    reviewed, [else stop].

    `--help` documents `dontAsk` as one of six `--permission-mode` choices;
    it says nothing about which of those choices deny by default and which
    auto-approve, so passing enum membership alone does not prove the
    direction `scripts/claude_review.py._claude_argv`'s docstring assumes.

    The installed cli embeds that per-mode semantics as plain text in its
    own binary (not surfaced by `--help`, but present in the same shipped
    artifact `--help` is read from). Reading it is a second, static read of
    ground truth -- not a live turn: a real turn needs auth this test
    cannot assume, and the module-level docstring above already rules out
    spending a model call from a unit test.
    """
    cli = _cli()
    real_path = Path(cli).resolve()
    try:
        blob = real_path.read_bytes()
    except OSError as exc:
        pytest.skip(
            f"UNAVAILABLE: cannot read the installed cli's binary at "
            f"{real_path} ({exc}), so this machine cannot introspect its "
            "permission-mode semantics. Reporting a pass here would be the "
            "tool answering a question it did not measure."
        )

    match = re.search(rb"'dontAsk' - ([^.]*\.)", blob)
    assert match, (
        "could not find the installed cli's own description of the "
        "'dontAsk' permission-mode choice inside its binary. Either the "
        "phrasing changed or this build strips it, and the direction "
        "scripts/claude_review.py assumes can no longer be proven "
        "statically -- update this probe or find another ground truth "
        "before trusting dontAsk to deny anything."
    )
    description = match.group(1).decode("utf-8", errors="replace")
    assert "deny" in description.lower(), (
        f"the installed cli describes 'dontAsk' as {description!r}, which "
        "does not say it denies by default. scripts/claude_review.py "
        "assumes --permission-mode dontAsk DENIES prompts instead of "
        "auto-approving them, and that assumption is what makes a hung or "
        "misbehaving review lane fail closed instead of silently approving "
        "something nobody reviewed."
    )
