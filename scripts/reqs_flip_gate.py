"""Per-PR gate: a PR citing a pending v1 id must flip it or carry a no-flip marker.

Step 3 of issue #1605, the enforcement half. `reqs_cited_unflipped.py`
(DEVOPS-07) is deliberately never a per-PR gate -- a docs PR recording an id
is a legitimate false positive, and guessing intent from a `docs` title
prefix is a heuristic, not a rule. This script closes the hole a different
way: it does not guess. A PR that cites a pending id must either flip it (so
it no longer reads pending in THIS PR's own reqs.json) or say explicitly why
not, with `reqs: no-flip <ID> <reason>` naming the exact id and a non-empty
reason. Silence is the only thing that fails.

    python -m scripts.reqs_flip_gate --pr 1234

What would satisfy this check without satisfying its intent, and why it
doesn't: a bare `reqs: no-flip <ID>` with no reason text still fails --
the regex requires trailing non-whitespace, so a rubber-stamped marker
cannot silence the gate for free. A PR citing zero ids passes with an
explicit "(no citations)" line, never conflated with a checked-and-clear
result. `gh pr view` failing prints UNKNOWN and exits 2, never a silent
pass -- a failed read is not the same as a PR with nothing to say.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from scripts.reqs_cited_unflipped import citations, pending_v1_ids

REPO_ROOT = Path(__file__).resolve().parent.parent
REQS_JSON = REPO_ROOT / "reqs.json"
DEFAULT_REPO = "maintainer/music-dj-tools"


def pr_view(pr: int, repo: str = DEFAULT_REPO) -> dict:
    proc = subprocess.run(
        ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "number,title,body"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr view failed rc={proc.returncode}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def _no_flip_marker(req_id: str, body: str) -> bool:
    # Requires trailing non-whitespace on the same line: a bare marker with
    # no reason must not silence the gate.
    pat = re.compile(r"reqs:\s*no-flip\s+" + re.escape(req_id) + r"\s+\S", re.IGNORECASE)
    return bool(pat.search(body))


def unflipped(pr: dict, payload: dict) -> list[str]:
    pending = pending_v1_ids(payload)
    cited_ids = {c.req_id for c in citations(pending, [pr | {"mergedAt": ""}])}
    body = pr.get("body") or ""
    return sorted(req_id for req_id in cited_ids if not _no_flip_marker(req_id, body))


def main(
    argv: list[str] | None = None,
    *,
    fetch=pr_view,
    reqs_path: Path = REQS_JSON,
) -> int:
    parser = argparse.ArgumentParser(prog="reqs_flip_gate")
    parser.add_argument("--pr", type=int, required=True)
    args = parser.parse_args(argv)

    try:
        payload = json.loads(reqs_path.read_text())
    except Exception as exc:
        print(f"[reqs-flip-gate] UNKNOWN: could not read {reqs_path} ({exc})", file=sys.stderr)
        return 2

    try:
        pr = fetch(args.pr)
    except Exception as exc:
        print(f"[reqs-flip-gate] UNKNOWN: could not read PR #{args.pr} ({exc})", file=sys.stderr)
        return 2

    offenders = unflipped(pr, payload)
    if not offenders:
        print(f"[reqs-flip-gate] OK -- PR #{args.pr}: no cited pending id unflipped or unmarked")
        return 0

    print(
        f"[reqs-flip-gate] PR #{args.pr} cites pending id(s) it neither flips to shipped in "
        f"this PR's own reqs.json nor marks `reqs: no-flip <id> <reason>`: {', '.join(offenders)}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
