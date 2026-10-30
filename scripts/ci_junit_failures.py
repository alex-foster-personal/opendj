"""Report a pytest shard's failing test ids where a reader can get them without the job log.

A red shard used to name its failures only inside the job log, which only a host holding a
logs-scoped token can read (cloud sessions cannot). This reads the shard's JUnit report and:

- prints one `::warning` workflow command per failing test (capped at `--max-annotations`,
  default 10, because GitHub keeps at most 10 annotations of a level per step), so the first
  failing ids are check-run annotations, readable over REST at `check-runs/{id}/annotations`.
  A warning, not an error: this step runs before the quarantine verdict, and a failure that
  quarantine later excuses must not leave a red annotation on a green shard;
- appends a "failing tests" section listing every id, uncapped, to the job summary, so a
  reader of the run page can reproduce exactly those (fleet-af `docs/testing-strategy.md`,
  rule 4);
- writes `failing_count=<n>` to `GITHUB_OUTPUT`.

Ids are deduplicated in report order (a test that fails and then errors in teardown is two
testcases with one id). Under GitHub Actions a missing `GITHUB_OUTPUT` or
`GITHUB_STEP_SUMMARY` is an error, exit 1, never a silent skip.

    python -m scripts.ci_junit_failures <junit.xml> --label "shard 2 of 5"

A report that is missing or cannot be parsed, or names no failing test when `--pytest-rc` is nonzero, is reported as UNKNOWN, never as zero failures:
`failing_count=unknown` and a `::warning`. Exit status is always 0, because the shard's own
step already carries its verdict; this step only names what failed.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path
from xml.etree import ElementTree

DEFAULT_MAX_ANNOTATIONS = 10
UNRESOLVED = "UNRESOLVED "


def node_id(classname: str, name: str, root: Path, file: str = "") -> str:
    """Turn a JUnit (classname, name) pair back into a pytest node id.

    pytest's xunit2 report drops the file path and writes a dotted classname such as
    `tests.scripts.test_x.TestY`. The module is the longest dotted prefix that is a file under
    `root`; whatever follows it is the class chain. A collection error has an empty classname
    and the module's dotted name as its name; it maps to the module's path, which is what
    `pytest` takes to reproduce it. The testcase `file` attribute, when present and a file
    under `root`, is used before any dotted guess. When nothing resolves (a deleted file, a
    report from another checkout) the id is never passed off as a node id: it is returned
    with the `UNRESOLVED ` prefix, kept out of the reproduce line and counted in the summary.
    """
    if file and (root / file).is_file():
        if not classname:
            return file
        module = Path(file).with_suffix("").as_posix().replace("/", ".")
        chain = classname[len(module) + 1 :].split(".") if classname.startswith(module + ".") else []
        return "::".join([file, *chain, name])
    if not classname:
        module = Path(*name.split(".")).with_suffix(".py")
        return module.as_posix() if (root / module).is_file() else f"{UNRESOLVED}{name}"
    parts = classname.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = Path(*parts[:cut]).with_suffix(".py")
        if (root / candidate).is_file():
            return "::".join([candidate.as_posix(), *parts[cut:], name])
    return f"{UNRESOLVED}{classname}::{name}"


def failing_ids(report: Path, root: Path) -> list[str]:
    """Every testcase with a <failure> or <error> child, as unique pytest node ids, in report order."""
    # The report is this job's own pytest output, never outside input, and the stdlib parser
    # resolves no external entities; the same call is in scripts/ci_test_selection.py.
    # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
    cases = ElementTree.parse(report).getroot().iter("testcase")
    ids: list[str] = []
    for case in cases:
        if case.find("failure") is None and case.find("error") is None:
            continue
        ids.append(node_id(case.get("classname", ""), case.get("name", ""), root, case.get("file", "")))
    return list(dict.fromkeys(ids))


def _escape(value: str) -> str:
    # Workflow-command data escaping, per the GitHub Actions toolkit.
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


class MissingTarget(RuntimeError):
    pass


def _append(env: str, text: str) -> None:
    target = os.environ.get(env)
    if not target:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            raise MissingTarget(f"{env} is not set, so this step's result has nowhere to go")
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path)
    parser.add_argument("--label", required=True, help='e.g. "shard 2 of 5"')
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--max-annotations", type=int, default=DEFAULT_MAX_ANNOTATIONS)
    parser.add_argument(
        "--pytest-rc",
        type=int,
        default=0,
        help="the shard's pytest exit status; nonzero with no failing testcase reads UNKNOWN",
    )
    args = parser.parse_args(argv)
    try:
        return _report(args)
    except MissingTarget as exc:
        print(f"\n::error title=failing tests not reported ({args.label})::{_escape(str(exc))}")
        return 1


def _unknown(label: str, why: str) -> int:
    print(f"\n::warning title=failing tests UNKNOWN ({label})::{_escape(why)}")
    _append("GITHUB_OUTPUT", "failing_count=unknown\n")
    _append(
        "GITHUB_STEP_SUMMARY",
        f"\n### Failing tests ({label}): UNKNOWN\n\n{why}, so the failing ids are only in the job log.\n",
    )
    return 0


def _report(args: argparse.Namespace) -> int:
    try:
        ids = failing_ids(args.report, args.root)
    except (OSError, ElementTree.ParseError) as exc:
        return _unknown(args.label, f"could not read JUnit report {args.report}: {exc}")
    if not ids and args.pytest_rc != 0:
        # pytest failed but named no testcase (exit 2 to 5: interrupted, internal error,
        # usage error, nothing collected): zero would hide a red shard.
        return _unknown(args.label, f"pytest exited {args.pytest_rc} but its JUnit report names no failing test")

    _append("GITHUB_OUTPUT", f"failing_count={len(ids)}\n")
    if not ids:
        print(f"[failing-tests] {args.label}: the JUnit report has no failing testcase")
        return 0

    for test_id in ids[: args.max_annotations]:
        print(f"\n::warning title=failing test ({args.label})::{_escape(test_id)}")
    lines = [f"\n### Failing tests ({args.label}): {len(ids)}\n", "", "```", *ids, "```"]
    runnable = [test_id for test_id in ids if not test_id.startswith(UNRESOLVED)]
    if runnable:
        shown = " ".join(shlex.quote(test_id) for test_id in runnable[:5])
        lines.append(f"\nReproduce only these: `pytest {shown}`")
        if len(runnable) > 5:
            lines.append(f"(first 5 of {len(runnable)}; the full list is above)")
    if len(runnable) < len(ids):
        lines.append(
            f"\n{len(ids) - len(runnable)} failing testcase(s) could not be mapped to a file in this "
            "checkout (marked UNRESOLVED); find them in the JUnit report artifact."
        )
    _append("GITHUB_STEP_SUMMARY", "\n".join(lines) + "\n")
    print(f"[failing-tests] {args.label}: {len(ids)} failing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
