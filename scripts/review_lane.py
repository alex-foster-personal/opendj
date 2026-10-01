"""Mechanics every CLI review lane shares: pin the head, fetch the diff,
anchor findings to it, and post them as one review.

WHY THIS IS A SEPARATE MODULE. There are now two lanes that drive a model
over a PR diff and post the result as review threads -- `sol_review.py`
(GPT-5.6 through the Codex CLI) and `claude_review.py` (Claude through the
`claude` CLI) -- and everything between "which PR" and "which model" is
identical between them. Every rule below was learned the hard way on the Sol
lane and is written down in the function that enforces it; a second copy of
any of them is a second place to forget one. `review_gh.py` was split out of
`review_coverage.py` for the same reason and is the layer below this one.

WHAT IS DELIBERATELY NOT HERE: the marker a lane writes (each lane owns its
own, in `review_sol.py` / `review_claude.py`, so coverage's reader and the
lane's writer cannot drift), the summary wording, and everything about
reaching a model. Those are the parts that genuinely differ.

Requirements (mini-PRD):
  / Refuse to act on a head the ref and the PR object disagree about.
    [if a review posts while the branch is mid-push then broken]
  / Never let a finding with an unpostable anchor vanish.
    [if a finding with a bad line number is dropped silently then broken]
  / Derive the verdict from the severity, never from the model's own wording.
    [if a P1 can post as NON-BLOCKING then broken]
  / Refuse to post if the head moved while the model was running.
    [if a review of the pre-push tree is marked as covering the push then broken]
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from scripts.review_gh import TriageError, _gh
from scripts.review_prompt import ALL_FENCES, RUN_ID_PLACEHOLDER, Fence

REPO = "maintainer/music-dj-tools"

#: Seconds to wait for the PR object to catch up with the branch ref.
HEAD_AGREE_TIMEOUT_S: int = 90

#: Severity to verdict. AGENTS.md defines the tiering; this is the only place
#: it is applied, so the two halves of a finding's marker cannot disagree.
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"P0", "P1"})

#: Shields colors matching the Codex bot's own badges, so every reviewer
#: renders identically in a thread list. `review_thread_parse._SEVERITY_BADGE`
#: reads the P-level straight out of this URL.
BADGE_COLORS: dict[str, str] = {"P0": "red", "P1": "orange", "P2": "yellow", "P3": "blue"}

#: Phrases `review_coverage.NOT_REVIEWED_MARKERS` reads as "this reviewer did
#: not really look". They are legitimate English, so model prose may contain
#: one; in a SUMMARY body that would make a real review self-report as a
#: non-review and fail its own coverage check. Withheld there by `withheld`,
#: kept verbatim in the inline thread where nothing parses them.
COVERAGE_POISON: tuple[str, ...] = (
    "rate limited",
    "review skipped",
    "trial expired",
    "no credits",
    "skipped:",
)

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DEBT_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

#: Characters `normalized_finding_title` keeps, because they carry a title's
#: MEANING rather than its rendering. Markdown decoration (`*`, `_`, backtick,
#: `#`) is deliberately absent: a debt heading and a model's own title differ
#: in exactly that way all the time, which is what the normalization is for.
_SEMANTIC_OPERATORS: frozenset[str] = frozenset("<>=+")

#: A `-` that is a sign rather than a dash: it precedes a digit and follows
#: something that is not a word character.
_SIGN = re.compile(r"(?<!\w)-(?=\d)")


def first_group(pattern: re.Pattern[str], text: str) -> str:
    """The pattern's first capture, or "unknown". Reporting-only: a missing
    model or token line must never fail a review that already happened."""
    match = pattern.search(text)
    return match.group(1) if match else "unknown"


def withheld(text: str) -> str:
    """Blank a title that would make a summary self-report as a non-review.

    See `COVERAGE_POISON`. The finding itself is untouched in its own thread;
    only its appearance in the coverage-scanned summary is replaced.
    """
    lowered = text.lower()
    if any(p in lowered for p in COVERAGE_POISON):
        return "(title withheld here; it contains a coverage-marker phrase. See the thread.)"
    return text


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    severity: str
    title: str
    detail: str

    @property
    def verdict(self) -> str:
        return "BLOCKING" if self.severity in BLOCKING_SEVERITIES else "NON-BLOCKING"

    def body(self, marker: str) -> str:
        """The thread body, shaped so `review_thread_parse` reads both halves.

        The badge gives `_severity` the P-level. The NEXT line must OPEN with
        the verdict, because `_blocking` anchors its match to the start of the
        headline and markdown images are stripped before that test -- a line
        reading "P1 BLOCKING: ..." parses as `unmarked` and tiers a blocker as
        an ordinary finding. The lane's own marker is passed in rather than
        built here: each lane owns its marker, and this module must not be
        able to write one lane's marker into another lane's thread.
        """
        color = BADGE_COLORS.get(self.severity, "lightgrey")
        badge = (
            f"![{self.severity} Badge]"
            f"(https://img.shields.io/badge/{self.severity}-{color}?style=flat)"
        )
        return (
            f"{badge}\n{self.verdict} {self.severity}: {self.title}\n\n{self.detail}\n\n{marker}\n"
        )


def normalized_finding_title(text: str) -> str:
    """Narrow title equivalence for independently rendered debt headings.

    Case, whitespace and DECORATIVE punctuation are presentation and collapse
    away. `_SEMANTIC_OPERATORS` do not: `Reject values > 5` and
    `Reject values < 5` are opposite findings, and folding them to one key
    lets a debt heading suppress the finding that contradicts it -- the worst
    outcome this module has, because the suppressed thread never appears
    anywhere for a human to notice.

    `-` is decorative rather than semantic, because this repo writes `--`
    where other prose writes a long dash, so a bare hyphen is far more often a
    dash than a minus. It survives only in SIGN position (before a digit,
    after a non-word character), which is where it is the negation half of
    `+` and where no dash ever appears.
    """
    signs = {match.start() for match in _SIGN.finditer(text)}
    return " ".join(
        "".join(_normalized_char(char, index in signs) for index, char in enumerate(text)).split()
    )


def _normalized_char(char: str, is_sign: bool) -> str:
    """One title character, padded so a kept operator is its own token."""
    if is_sign or char in _SEMANTIC_OPERATORS:
        return f" {char} "
    return char.casefold() if char.isalnum() else " "


def debt_logged_findings(
    findings: list[Finding], debt_text: str
) -> tuple[list[Finding], list[Finding]]:
    """Return (post, suppressed), preserving blockers and uncertain matches.

    Title equality after only presentation normalization is intentionally
    stricter than semantic similarity. A finding with new meaning must post.
    """
    headings = {normalized_finding_title(heading) for heading in _DEBT_HEADING.findall(debt_text)}
    post: list[Finding] = []
    suppressed: list[Finding] = []
    for finding in findings:
        matched = normalized_finding_title(finding.title) in headings
        if finding.severity not in BLOCKING_SEVERITIES and matched:
            suppressed.append(finding)
        else:
            post.append(finding)
    return post, suppressed


# ----- head pinning -------------------------------------------------------


def _branch(pr: str) -> str:
    """The PR's head branch, refusing a PR that is not open.

    A merged or closed PR usually has no branch left, and `git ls-remote`
    answers a deleted ref with exit 0 and empty output -- indistinguishable
    from a network problem if the state is not checked first. Observed on
    #1224 (Fri 5 Sep 2026), which merged between being picked as a review
    target and the lane running.
    """
    query = '.state + " " + .headRefName'
    args = ["pr", "view", pr, "--repo", REPO, "--json", "state,headRefName", "-q", query]
    state, branch = _gh(args).split()
    if state != "OPEN":
        raise TriageError(f"PR #{pr} is {state}, not OPEN; there is nothing left to review")
    return branch


def _ref_sha(branch: str) -> str:
    """The branch tip straight from the remote ref, via `git ls-remote`.

    Deliberately not `gh pr view --json headRefOid`: the PR object lags the
    ref, and this repo has already shipped one bug from gating on the lagging
    copy. The remote URL is spelled out rather than using `origin`, so the
    answer does not depend on which checkout the lane happens to run from.
    """
    proc = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{REPO}.git", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise TriageError(
            f"git ls-remote found no refs/heads/{branch}: {proc.stderr.strip() or '<empty>'}"
        )
    return proc.stdout.split()[0]


def _pr_head(pr: str) -> str:
    args = ["pr", "view", pr, "--repo", REPO, "--json", "headRefOid", "-q", ".headRefOid"]
    return _gh(args).strip()


def pinned_head(pr: str) -> str:
    """The head SHA, taken from the REF and confirmed against the PR object.

    Two readings of the same fact, and they disagree for a real reason: the
    PR object's `headRefOid` lags the branch ref for seconds to minutes after
    a push. Reviewing the ref's SHA while coverage measures the PR object's
    would post evidence at a SHA the gate is not asking about, so the review
    would be invisible to the very gate it exists to satisfy. Wait for them to
    agree, then pin; never average them, never pick one.
    """
    branch = _branch(pr)
    deadline = time.monotonic() + HEAD_AGREE_TIMEOUT_S
    while True:
        ref, obj = _ref_sha(branch), _pr_head(pr)
        if ref == obj:
            return ref
        if time.monotonic() >= deadline:
            raise TriageError(
                f"branch {branch} is at {ref} but PR #{pr} still reports {obj} after "
                f"{HEAD_AGREE_TIMEOUT_S}s. The PR is mid-push; re-run once it settles."
            )
        time.sleep(5)


# ----- the diff -----------------------------------------------------------


def diff_of(pr: str) -> str:
    return _gh(["api", f"repos/{REPO}/pulls/{pr}", "-H", "Accept: application/vnd.github.v3.diff"])


# Machine-written data that CI validates mechanically rather than by reading.
# .test_durations is pytest-split's timing table: tests/scripts/
# test_ci_shard_matrix.py checks it is JSON with >= 3000 non-negative numbers.
# A full refresh is ~800 KB of diff, which alone exceeds the reviewers' size
# cap and blocks the PR from ever being reviewed (#2880, Tue 15 Sep 2026).
#
# apps/webui/openapi.json and apps/webui/frontend/src/lib/api-types.ts are
# the same class of problem, hit for the second time on PR #3679 (Thu 24 Sep
# 2026): a rebase onto a fast-moving main needs `just openapi-dump` +
# `pnpm run api:gen` to pick up new backend routes, and that regeneration
# alone can be several thousand lines. Both files are validated byte-for-byte
# by dedicated CI jobs that regenerate them from the checked-out code and
# diff (ci.yml "Contract drift - openapi.json" and "Contract drift - TS
# client"), so nothing here is unread by a mechanical check; it is only
# unread by the LLM reviewers, which is what this set exists to declare.
GENERATED_DATA_PATHS: frozenset[str] = frozenset(
    {
        ".test_durations",
        "apps/webui/openapi.json",
        "apps/webui/frontend/src/lib/api-types.ts",
    }
)


# Files generated only BETWEEN markers. docs/perf/performance-register.md renders its flag
# table from docs/perf/register.d/ fragments, and `python -m scripts.perf_register --check`
# pins that block byte for byte, but the aspect register and prose around it are
# hand-written and no check reads them. So its section is dropped only when EVERY changed
# line is a flag-table line that its own hunk PROVES lies between the markers; one other
# line, or one it cannot place, keeps the whole section reviewed. The fragments themselves,
# 0000-before-fragments.md included, are hand-written rows and are never skipped.
@dataclass(frozen=True)
class GeneratedBlock:
    begin_prefix: str
    end: str
    is_generated_line: Callable[[str], bool]

    def is_marker(self, text: str) -> bool:
        return text.startswith(self.begin_prefix) or text == self.end


_PERF_FLAG_ROW = re.compile(r"^\| (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} [A-Z][a-z]{2} \d{4} \|")
_PERF_FLAG_TABLE_HEADER = frozenset(
    {
        "| Date | Agent / PR | Aspect | Flag | Triage |",
        "|------|------------|--------|------|--------|",
    }
)


def _is_perf_flag_table_line(text: str) -> bool:
    return bool(_PERF_FLAG_ROW.match(text)) or text in _PERF_FLAG_TABLE_HEADER


GENERATED_BLOCK_PATHS: dict[str, GeneratedBlock] = {
    "docs/perf/performance-register.md": GeneratedBlock(
        begin_prefix="<!-- BEGIN GENERATED: rows from docs/perf/register.d/",
        end="<!-- END GENERATED -->",
        is_generated_line=_is_perf_flag_table_line,
    ),
}
_FILE_HEADER = re.compile(r'^diff --git a/(?:"([^"]+)"|(\S+)) b/(?:"([^"]+)"|(\S+))$')
_FILE_HEADER_QUOTED = re.compile(r'^diff --git "a/([^"]+)" "b/([^"]+)"$')


def _parse_file_header(line: str) -> tuple[str, str] | None:
    if match := _FILE_HEADER_QUOTED.match(line):
        return match.group(1), match.group(2)
    if match := _FILE_HEADER.match(line):
        a_path = match.group(1) or match.group(2)
        b_path = match.group(3) or match.group(4)
        return a_path, b_path
    return None


def reviewed_paths_in_diff(diff: str) -> frozenset[str]:
    """Every b/ path from a successfully parsed `diff --git` header."""
    paths: set[str] = set()
    for line in diff.split("\n"):
        if not line.startswith("diff --git "):
            continue
        parsed = _parse_file_header(line)
        if not parsed:
            raise TriageError(f"unparsed diff header (refusing to guess skip state): {line!r}")
        paths.add(parsed[1])
    return frozenset(paths)


def split_generated_data(diff: str) -> tuple[str, frozenset[str]]:
    """The diff minus whole-file sections for GENERATED_DATA_PATHS, and those paths.

    A section is dropped only when BOTH sides name the same generated path, so a
    rename from a source file into one stays in the reviewed diff. The caller
    must name the dropped paths in the review marker: coverage reads the marker,
    not the prose.

    Requirements:
      / Split on ``\\n`` only so a forged header inside a ``\\r`` cannot start a
        skip section. [if ``splitlines`` is used then broken]
      / Every ``diff --git `` line must parse or raise ``TriageError``. [if an
        unparsed header inherits skip state then broken]
      / Quoted git path headers parse like unquoted ones. [if they inherit skip
        state then broken]
    """
    kept: list[str] = []
    skipped_paths: set[str] = set()
    section: list[str] = []
    skipping = False
    block: GeneratedBlock | None = None

    def flush() -> None:
        nonlocal section, skipping
        if section and block is not None and _only_generated_changes(section, block):
            skipping = True
            skipped_paths.add(section_path)
        if section and not skipping:
            kept.extend(section)
        section = []

    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            flush()
            parsed = _parse_file_header(line)
            if not parsed:
                raise TriageError(f"unparsed diff header (refusing to guess skip state): {line!r}")
            a_path, b_path = parsed
            section = [line]
            section_path = b_path
            skipping = a_path == b_path and b_path in GENERATED_DATA_PATHS
            block = GENERATED_BLOCK_PATHS.get(b_path) if a_path == b_path else None
            if skipping:
                skipped_paths.add(b_path)
        else:
            section.append(line)

    flush()
    return "\n".join(kept), frozenset(skipped_paths)


def _only_generated_changes(section: list[str], block: GeneratedBlock) -> bool:
    """True when the section has hunks and every changed line is generated output its own
    hunk proves lies inside the block.

    A marker is not a generated line, so in a skippable section markers appear only as
    context, on both sides alike. A hunk is contiguous, so a changed line is inside exactly
    when the nearest marker above it in the hunk is BEGIN or the nearest below it is END.
    A line with no marker in view cannot be placed and keeps the section reviewed.
    """
    starts = [i for i, line in enumerate(section) if line.startswith("@@")]
    if not starts:
        return False
    for start, stop in zip(starts, [*starts[1:], len(section)], strict=True):
        hunk = [line for line in section[start + 1 : stop] if line[:1] in (" ", "+", "-")]
        if not all(_changed_line_is_placed_inside(hunk, i, block) for i in range(len(hunk))):
            return False
    return any(line[:1] in ("+", "-") for line in section[starts[0] + 1 :])


def _changed_line_is_placed_inside(hunk: list[str], index: int, block: GeneratedBlock) -> bool:
    line = hunk[index]
    text = line[1:]
    if line[:1] == " ":
        return True
    if not block.is_generated_line(text):
        return False
    above = next((h[1:] for h in reversed(hunk[:index]) if block.is_marker(h[1:])), None)
    below = next((h[1:] for h in hunk[index + 1 :] if block.is_marker(h[1:])), None)
    return (above is not None and above.startswith(block.begin_prefix)) or below == block.end


def reviewable_diff(pr: str) -> tuple[str, list[str]]:
    filtered, skipped = split_generated_data(diff_of(pr))
    return filtered, sorted(skipped)


def unreviewed_note(dropped: list[str]) -> list[str]:
    if not dropped:
        return []
    names = ", ".join(f"`{path}`" for path in dropped)
    return [
        "",
        f"Not reviewed: {names}, generated data that CI validates mechanically "
        "(scripts/review_lane.py GENERATED_DATA_PATHS, GENERATED_BLOCK_PATHS).",
    ]


def skipped_paths_match_diff(marker_skipped: frozenset[str], diff: str) -> str | None:
    """Return a coverage failure reason when marker skipped paths disagree with `diff`."""
    _, expected = split_generated_data(diff)
    if marker_skipped == expected:
        return None
    return (
        f"marker skipped={','.join(sorted(marker_skipped)) or '(none)'} "
        f"but diff generated-data sections are "
        f"{','.join(sorted(expected)) or '(none)'}"
    )


def certified_reviewed_paths(diff: str, skipped: frozenset[str]) -> frozenset[str]:
    """Non-generated changed paths the lane read after dropping `skipped`."""
    return reviewed_paths_in_diff(diff) - skipped


def cli_lane_marker_skipped_mismatch(lane: str, body: str, diff: str) -> str | None:
    """Coverage helper: marker `skipped=` must match generated-data sections in `diff`."""
    if lane == "Claude":
        from scripts.review_claude import marker_skipped_mismatch

        return marker_skipped_mismatch(body, diff)
    if lane == "Sol":
        from scripts.review_sol import marker_skipped_mismatch

        return marker_skipped_mismatch(body, diff)
    from scripts.review_subscription import LANES_BY_NAME

    if subscription_lane := LANES_BY_NAME.get(lane):
        return subscription_lane.marker_skipped_mismatch(body, diff)
    return None


def evidence_skipped_paths_ok(
    lane: str, bodies: tuple[str, ...], pr_diff: str | None
) -> str | None:
    if not pr_diff:
        return None
    for body in bodies:
        if reason := cli_lane_marker_skipped_mismatch(lane, body, pr_diff):
            return reason
    return None


def anchorable_lines(diff: str) -> dict[str, set[int]]:
    """RIGHT-side line numbers a review comment may be attached to.

    GitHub rejects the whole review with a 422 if ANY comment names a line
    outside the diff, so one bad anchor loses every finding in the batch. This
    is the pure half of that defense; `split_by_anchor` is what acts on it.
    Added and context lines both count; removed lines have no right-side
    number and are deliberately absent.
    """
    lines: dict[str, set[int]] = {}
    path, cursor = "", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            path, cursor = raw[6:], 0
            lines.setdefault(path, set())
        elif (hunk := _HUNK.match(raw)) and path:
            cursor = int(hunk.group(1))
        elif path and cursor:
            if raw.startswith(("+", " ")):
                lines[path].add(cursor)
                cursor += 1
            elif raw.startswith("-"):
                continue
            else:
                cursor = 0
    return lines


def split_by_anchor(
    findings: list[Finding], anchors: dict[str, set[int]]
) -> tuple[list[Finding], list[Finding]]:
    """(postable inline, carried in the summary instead)."""
    inline: list[Finding] = []
    carried: list[Finding] = []
    for finding in findings:
        ok = finding.line is not None and finding.line in anchors.get(finding.path, set())
        (inline if ok else carried).append(finding)
    return inline, carried


# ----- the model's reply --------------------------------------------------


def parse_findings(output: str, run_id: str, fence: Fence, max_findings: int) -> list[Finding]:
    """Pull THIS RUN's fenced JSON out of a model transcript.

    Every block is considered and the last one CARRYING THIS RUN'S ID wins.
    Both halves matter. Last, because a model may restate the shape while
    reasoning. This run's id, because the transcript may also contain blocks
    the model never wrote: `codex exec` echoes the prompt, so the example
    block is always in there, and a diff that touches these files carries
    more. Accepting the last block unconditionally means a reply with no block
    at all silently degrades into posting the EXAMPLE as findings -- a
    fabricated review, which is strictly worse than no review.

    EVERY contract violation below used to read as "clean" on the Sol lane. A
    missing `findings` key defaulted to an empty list, and a list longer than
    the cap was silently sliced. Both discard real findings while still
    posting a current-head marker that satisfies coverage, which is the exact
    shape of a check that cannot fail.
    """
    block = re.compile(re.escape(fence.open) + r"(.*?)" + re.escape(fence.close), re.DOTALL)
    blocks = [b for b in block.findall(output) if RUN_ID_PLACEHOLDER not in b]
    mine = [b for b in blocks if run_id in b] or _bare_replies(output, run_id)
    if not mine:
        raise TriageError(
            f"no {fence.open} block, and no standalone JSON object whose run is {run_id}, carrying "
            f"run id {run_id} in the model output; the model did not follow the reply "
            f"contract. Tail:\n{output[-800:]}"
        )
    try:
        payload = json.loads(mine[-1].strip())
    except json.JSONDecodeError as exc:
        # Without this the lane dies with a traceback and exit 1, which reads
        # as an ordinary failure rather than "could not review". Every other
        # contract violation here is a TriageError for that reason, and a
        # truncated or malformed block is the likeliest of them.
        raise TriageError(
            f"the {fence.open} block for run {run_id} is not valid JSON ({exc}); "
            f"the model did not follow the reply contract. Block:\n{mine[-1][-800:]}"
        ) from exc
    if payload.get("run") != run_id:
        raise TriageError(f"reply block names run {payload.get('run')!r}, not {run_id!r}")
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        raise TriageError(f"reply carries no findings list (got {type(raw_findings).__name__})")
    if len(raw_findings) > max_findings:
        raise TriageError(
            f"reply carries {len(raw_findings)} findings, over the {max_findings} cap; "
            "truncating here would drop findings the model actually made"
        )
    findings = []
    for raw in raw_findings:
        severity = str(raw.get("severity", "")).upper()
        if severity not in BADGE_COLORS:
            raise TriageError(f"finding carries an unknown severity {severity!r}: {raw}")
        for field in ("path", "title"):
            if not str(raw.get(field, "")).strip():
                raise TriageError(f"finding is missing a {field}: {raw}")
        line = raw.get("line")
        findings.append(
            Finding(
                path=str(raw["path"]),
                line=int(line) if isinstance(line, int) else None,
                severity=severity,
                title=str(raw["title"]).strip(),
                detail=str(raw.get("detail", "")).strip(),
            )
        )
    return findings


def _bare_replies(output: str, run_id: str) -> list[str]:
    """Top-level JSON objects in `output` whose "run" IS this run's id.

    The fence is a convenience for finding the reply among the CLI's own log
    lines; the RUN ID is what makes a reply this run's. It is minted fresh
    per run, so the echoed prompt (whose example carries the placeholder) and
    any block quoted in a diff cannot carry it. Measured Thu 10 Sep 2026 on
    #1717: two Sol runs in a row printed a correct `{"run": ..., "findings":
    [...]}` without the fence and were discarded, which is a real review
    thrown away and coverage reported as MISS.

    Only an object whose "run" EQUALS the id counts, never one that merely
    contains the id somewhere, and only when no fenced block for this run
    exists, so a fenced answer still wins. Text inside ANY lane's fence is
    skipped: the fences are per lane so another lane's block (quoted source,
    tests, or a mis-fenced reply) is never this lane's answer, and the
    unfenced path must not reopen that. Each candidate is re-serialized so the
    caller parses it exactly as it parses a fenced block.

    The object must also STAND ALONE: it opens a line and nothing but
    whitespace follows it on its last line, and it is not inside a Markdown
    code block. Any decodable substring used to count, so a model writing "I
    would have returned {...}, but could not review" posted a clean review
    (Codex review, #1746). A candidate too deeply nested to decode is skipped
    like any other undecodable one, rather than crashing the lane on a diff
    that carries one (Codex review, #1746).
    """
    for fence in ALL_FENCES:
        fenced = re.escape(fence.open) + r".*?" + re.escape(fence.close)
        output = re.sub(fenced, "", output, flags=re.DOTALL)
    output = _MARKDOWN_CODE_BLOCK.sub("", output)
    decoder = json.JSONDecoder()
    found: list[str] = []
    for match in _LINE_OPENING_BRACE.finditer(output):
        try:
            value, end = decoder.raw_decode(output, match.end() - 1)
        except (json.JSONDecodeError, RecursionError):
            continue
        rest_of_last_line = output[end:].split("\n", 1)[0]
        if rest_of_last_line.strip() or not isinstance(value, dict):
            continue
        if value.get("run") == run_id:
            found.append(json.dumps(value))
    return found


#: A brace that opens its line: where a standalone reply object can start.
_LINE_OPENING_BRACE = re.compile(r"^[ \t]*\{", re.MULTILINE)
#: A Markdown code block quotes JSON; it is never the reply itself.
_MARKDOWN_CODE_BLOCK = re.compile(r"^[ \t]*```.*?^[ \t]*```", re.DOTALL | re.MULTILINE)


# ----- posting ------------------------------------------------------------


def _require_post_head_unchanged(pr: str, reviewed_sha: str, current_sha: str) -> None:
    if current_sha != reviewed_sha:
        raise TriageError(
            f"PR #{pr} moved from {reviewed_sha} to {current_sha} while the review ran; "
            "nothing posted. Re-run against the new head."
        )


def post_review(
    pr: str,
    sha: str,
    summary: str,
    inline: list[Finding],
    marker: str,
    tag: str,
) -> str:
    """Post one review: a summary plus an inline thread per anchored finding.

    `inline` is what `split_by_anchor` said can carry a line anchor; GitHub
    rejects the WHOLE review with a 422 if any one comment names a line
    outside the diff, so the split happens before this call and the findings
    that failed it travel in the summary the caller built.

    The head is re-read here. It was pinned before a model run that can last
    twenty minutes, and a branch that advanced in that window would otherwise
    get a review of the tree BEFORE the push, marked as covering the tree
    after it -- the stale-round failure issue #1016 already fixed on the
    reading side.
    """
    _require_post_head_unchanged(pr, sha, pinned_head(pr))
    payload = {
        "commit_id": sha,
        "event": "COMMENT",
        "body": summary,
        "comments": [
            {"path": f.path, "line": f.line, "side": "RIGHT", "body": f.body(marker)}
            for f in inline
        ],
    }
    endpoint = f"repos/{REPO}/pulls/{pr}/reviews"
    out = _gh(["api", endpoint, "--input", "-", "-q", ".html_url"], payload, as_human=True)
    print(f"{tag} posted {len(inline)} inline thread(s)")
    return out.strip()
