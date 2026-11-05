"""JEV advisory review of the branch diff at push: three judgment calls a regex cannot make.

the maintainer, Tue 3 Nov 2026: put "powerful subjective catches that prevent CI failures cheaply at the
moment of ignoring a rule, probably the commit, vs IN a merge queue". The rubric is
`(private path)`; its measured precision is in `(private path)`.

    doppler run -p general -c dev_personal -- uv run --no-sync python -m scripts.jev_pre_push --advisory
    ... --pr 8429 --json out.json    # review a PR's diff instead of this branch (the eval path)

What is asked, and of which text:
- `behaviour_change` of a summary (commit messages or PR title and body, plus the file list):
  is this a feature or fix? Flagged only when the diff also adds or amends no requirement id, which this
  script reads exactly from `.planning/REQUIREMENTS.md` (a presence check is no job for JEV).
- `only_executes` of each added Python test function: does it run code and check nothing?
- `masks_failure` of each added non-test hunk that has an except, a catch or a default-on-miss
  idiom: does it hide a failure from the caller?

Hard rules, from the jev skill: every question has a flagging and a clean control, and they run
first; one wrong answer makes the run UNKNOWN. UNKNOWN (no key, a failed call, a bad answer) is
exit 3 and never a pass, `--advisory` included. Flags are pointers to look at, not verdicts:
`--advisory` turns a flag's exit 1 into 0, and `just pre-push` only runs this when asked.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUBRIC_PATH = REPO / "ops" / "quality" / "jev" / "pre-push.toml"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = os.environ.get("JEV_MODEL", "~typesafe/jev-latest")
MAX_STATE_CHARS = 12000  # well under JEV's 32k-token context at 3.5 chars a token
INPUTS = ("summary", "test", "hunk")
REQUIREMENTS = ".planning/REQUIREMENTS.md"
# A requirement bullet on an added line. Ids may carry a letter suffix (`OFFLOAD-8598b`, #8696).
NEW_ID = re.compile(r"^\+\s*- (?:\[[ x]\] )?\*\*([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d+[a-z]*)\*\*")
# Idioms that can swallow a failure. A pre-filter only: JEV judges whether the hunk masks one.
# `??` is left out: on an optional field it hides no error (3 of 11 flags, #8598/#8601, round 0).
MASKING = re.compile(
    r"\bexcept\b|\bcatch\s*[({]|\.catch\(|\|\|\s*true\b|2>/dev/null|\bor\s+(?:''|\"\"|\[\]|\{\}|None|0)\b"
    r"|\.get\([^()]*,|getattr\([^()]*,[^()]*,|contextlib\.suppress|\bpass\s*$"
)
# Paths that are records, not behaviour: a diff of only these asks no behaviour question (#8590, #8661).
RECORD_PREFIXES = ("specs/", "docs/", "app_docs/", "handoffs/", ".planning/", "blog/")
SOURCE_SUFFIXES = (".py", ".ts", ".js", ".mjs", ".svelte", ".sh", ".rs")
Transport = Callable[[dict], dict]


@dataclass(frozen=True)
class Question:
    id: str
    input: str
    ask: str
    flag_when: str = "yes"


@dataclass(frozen=True)
class Control:
    label: str
    input: str
    text: str
    expect: dict[str, str]


@dataclass(frozen=True)
class Rubric:
    questions: tuple[Question, ...]
    controls: tuple[Control, ...]
    context: str
    threshold: float


@dataclass(frozen=True)
class Item:
    input: str
    subject: str
    text: str


@dataclass(frozen=True)
class Flag:
    question: str
    subject: str
    p_yes: float


@dataclass
class Report:
    flags: list[Flag] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    answers: list[dict] = field(default_factory=list)
    models: set[str] = field(default_factory=set)
    cost_usd: float = 0.0

    def exit_code(self, *, advisory: bool) -> int:
        if self.unknown:
            return 3
        if self.flags and not advisory:
            return 1
        return 0


# ----- rubric -----


def load_rubric(path: Path) -> Rubric:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    questions = tuple(Question(q["id"], q["input"], q["ask"], q.get("flag_when", "yes")) for q in raw["questions"])
    controls = tuple(Control(c["label"], c["input"], c["text"], dict(c["expect"])) for c in raw.get("controls", []))
    for q in questions:
        if q.input not in INPUTS:
            raise ValueError(f"question {q.id}: input {q.input!r} is not one of {INPUTS}")
        seen = {c.expect[q.id] for c in controls if q.id in c.expect and c.input == q.input}
        if seen != {"yes", "no"}:
            raise ValueError(
                f"question {q.id} needs one control expecting yes and one expecting no, has {sorted(seen)}"
            )
    return Rubric(questions, controls, raw.get("context", "").strip(), float(raw.get("threshold", 0.5)))


# ----- pure: what the diff gives JEV -----


def touched_requirement_ids(diff: str) -> set[str]:
    """Ids on lines the diff adds to REQUIREMENTS.md: a new id, or an amended one (#8696)."""
    return {
        m.group(1)
        for path, lines in _files(diff)
        if path == REQUIREMENTS
        for line in lines
        if (m := NEW_ID.match(line))
    }


def _files(diff: str) -> list[tuple[str, list[str]]]:
    """(new path, its diff lines) per file; a deleted file keeps its old path."""
    out: list[tuple[str, list[str]]] = []
    for block in re.split(r"^diff --git ", diff, flags=re.MULTILINE)[1:]:
        lines = block.splitlines()
        new = next((ln[6:] for ln in lines if ln.startswith("+++ b/")), None)
        old = next((ln[6:] for ln in lines if ln.startswith("--- a/")), None)
        path = new or old
        if path is not None:
            out.append((path, lines))
    return out


def _hunks(lines: list[str]) -> list[tuple[int, list[str]]]:
    hunks: list[tuple[int, list[str]]] = []
    for line in lines:
        if line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
            hunks.append((int(m.group(1)) if m else 0, []))
        elif hunks and not line.startswith(("+++", "---")):
            hunks[-1][1].append(line)
    return hunks


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith("tests/") or "/tests/" in path or name.startswith("test_") or ".test." in name


def _added_tests(path: str, lines: list[str]) -> list[Item]:
    """Each wholly added `def test_*` in a Python test file, as its own text."""
    items: list[Item] = []
    current: list[str] | None = None
    name, indent = "", 0

    def close() -> None:
        nonlocal current
        if current is not None:
            items.append(Item("test", f"{path}::{name}", "\n".join(current)[:MAX_STATE_CHARS]))
            current = None

    for line in lines:
        if not line.startswith("+") or line.startswith("+++"):
            context = line[1:]
            if current is not None and context.strip() and len(context) - len(context.lstrip()) > indent:
                current = None  # the body goes on in lines this diff did not add (#8604)
            close()
            continue
        body = line[1:]
        level = len(body) - len(body.lstrip())
        if current is not None and body.strip() and level <= indent:
            close()
        # Indent 0 or 4: a module test or a method. Deeper is a test inside a string fixture (#8666).
        if m := re.match(r"( {0,4})(?:async\s+)?def\s+(test_\w+)", body):
            current, name, indent = [body], m.group(2), len(m.group(1))
        elif current is not None:
            current.append(body)
    close()
    return items


def _masking_hunks(path: str, lines: list[str]) -> list[Item]:
    items: list[Item] = []
    for start, hunk in _hunks(lines):
        added = [ln for ln in hunk if ln.startswith("+")]
        if any(MASKING.search(ln[1:]) for ln in added):
            text = "\n".join(ln[1:] if ln[:1] in "+ " else ln for ln in hunk if not ln.startswith("-"))
            items.append(Item("hunk", f"{path}@{start}", f"FILE {path}\n{text}"[:MAX_STATE_CHARS]))
    return items


def branch_items(diff: str, summary: str) -> list[Item]:
    files = _files(diff)
    listing = "\n".join(f"  {path}" for path, _ in files)
    behaviour = any(not (path.startswith(RECORD_PREFIXES) or path.endswith(".md")) for path, _ in files)
    summary_text = f"{summary.strip()}\n\nFILES CHANGED:\n{listing}"[:MAX_STATE_CHARS]
    items = [Item("summary", "summary", summary_text)] if behaviour else []
    for path, lines in files:
        if path.endswith(".py") and _is_test_path(path):
            items.extend(_added_tests(path, lines))
        elif path.endswith(SOURCE_SUFFIXES) and not _is_test_path(path):
            items.extend(_masking_hunks(path, lines))
    return items


# ----- asking JEV -----


def _payload(rubric: Rubric, questions: list[Question], text: str, subject: str) -> dict:
    return {
        "model": MODEL,
        "state": {"subject": subject, "text": text},
        "questions": {
            q.id: {"type": "noul", "instructions": [rubric.context, q.ask] if rubric.context else q.ask}
            for q in questions
        },
    }


def _probabilities(reply: dict, questions: list[Question]) -> dict[str, float]:
    """P(yes) per question, or raise: a missing or out-of-range answer is never read as a no."""
    probs = {q.id: float(reply["answers"][q.id]["noul"]) for q in questions}
    if not all(0.0 <= p <= 1.0 for p in probs.values()):
        raise ValueError(f"probability out of range: {probs}")
    return probs


def _ask(
    send: Transport, rubric: Rubric, input_kind: str, text: str, subject: str, report: Report
) -> dict[str, float] | None:
    questions = [q for q in rubric.questions if q.input == input_kind]
    try:
        reply = send(_payload(rubric, questions, text, subject))
        probs = _probabilities(reply, questions)
    except Exception as exc:  # every failure is recorded as UNKNOWN with its cause, never a pass
        report.unknown.append(f"{subject}: {type(exc).__name__}: {exc}")
        return None
    report.models.add(str(reply.get("model", "")))
    report.cost_usd += float((reply.get("usage") or {}).get("cost") or 0.0)
    report.answers.append({"subject": subject, "input": input_kind, "p_yes": probs})
    return probs


def review(rubric: Rubric, items: list[Item], *, new_ids: set[str], send: Transport, workers: int = 8) -> Report:
    report = Report()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        control_probs = list(
            pool.map(lambda c: _ask(send, rubric, c.input, c.text, f"control:{c.label}", report), rubric.controls)
        )
        for control, probs in zip(rubric.controls, control_probs, strict=True):
            if probs is None:
                continue  # already UNKNOWN with its cause
            for qid, want in control.expect.items():
                got = "yes" if probs[qid] >= rubric.threshold else "no"
                if got != want:
                    report.unknown.append(
                        f"control {control.label}: {qid} answered {got} (P={probs[qid]:.2f}), expected {want}"
                    )
        if report.unknown:
            return report
        item_probs = list(pool.map(lambda i: _ask(send, rubric, i.input, i.text, i.subject, report), items))
    for item, answer in zip(items, item_probs, strict=True):
        if answer is None:
            continue  # already UNKNOWN with its cause
        for q in (q for q in rubric.questions if q.input == item.input):
            yes = answer[q.id] >= rubric.threshold
            if (yes if q.flag_when == "yes" else not yes) and not (q.id == "behaviour_change" and new_ids):
                report.flags.append(Flag(q.id, item.subject, answer[q.id]))
    return report


def _openrouter(api_key: str) -> Transport:
    def send(payload: dict) -> dict:
        request = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "jev_pre_push",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())

    return send


# ----- sources -----


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout


def branch_source(base: str) -> tuple[str, str]:
    return _git("diff", "-M", f"{base}...HEAD"), _git("log", "--format=%B", f"{base}..HEAD")


def pr_source(number: int) -> tuple[str, str]:
    diff = subprocess.run(
        ["gh", "pr", "diff", str(number)], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    view = subprocess.run(
        ["gh", "pr", "view", str(number), "--json", "title,body"], cwd=REPO, capture_output=True, text=True, check=True
    )
    meta = json.loads(view.stdout)
    return diff, f"{meta['title']}\n\n{meta['body']}"


# ----- cli -----


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--advisory", action="store_true", help="a flag exits 0; UNKNOWN still exits 3")
    parser.add_argument("--json", type=Path, help="write every probability, the served model and the cost")
    args = parser.parse_args(argv)
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print(
            "[jev-pre-push] UNKNOWN: no OPENROUTER_API_KEY; run under doppler run -p general -c dev_personal --. Skipped."
        )
        return 3
    diff, summary = pr_source(args.pr) if args.pr else branch_source(args.base)
    items = branch_items(diff, summary)
    report = review(load_rubric(RUBRIC_PATH), items, new_ids=touched_requirement_ids(diff), send=_openrouter(key))
    for flag in report.flags:
        print(f"[jev-pre-push] FLAG {flag.question} {flag.subject} P(yes)={flag.p_yes:.2f}")
    for cause in report.unknown:
        print(f"[jev-pre-push] UNKNOWN {cause}")
    verdict = "UNKNOWN" if report.unknown else ("FLAG" if report.flags else "CLEAN")
    print(
        f"[jev-pre-push] {verdict}: {len(items)} items, {len(report.flags)} flags, advisory={args.advisory}, "
        f"model={','.join(sorted(report.models)) or MODEL}, cost=${report.cost_usd:.5f}"
    )
    if args.json:
        args.json.write_text(
            json.dumps({**asdict(report), "models": sorted(report.models), "verdict": verdict}, indent=2)
        )
    return report.exit_code(advisory=args.advisory)


if __name__ == "__main__":
    sys.exit(main())
