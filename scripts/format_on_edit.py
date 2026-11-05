"""Format the one file an agent just edited with its pinned formatter: an opt-in Claude Code PostToolUse hook.

    python3 -m scripts.format_on_edit < payload.json     # run from the repo root (issue #4456 part 9)

Reads the PostToolUse JSON on stdin and formats `tool_input.file_path`, the way the ratified layers say
("rewrite only inside the agent's own edit loop, check everywhere else", (private path)):
ruff from ops/quality/requirements.txt for .py/.pyi, the frontend's pinned prettier for ts, tsx, mts, cts,
js, mjs, cjs and svelte under apps/webui/frontend (its .prettierignore decides; .css, .json and the rest
are ignored there), and the pinned shfmt `-i 4 -ci` for *.sh, *.bash and sh/bash shebang scripts. The
settings.json snippet is in the spec; nothing here installs it.

Requirements (mini-PRD):
  format_on_edit  ✔︎
    Formats exactly the edited file, in place, when it is in this checkout and of a formatted type, and
    leaves the formatter backlog alone: a file whose committed (HEAD) version is not formatted yet belongs
    to part 6, which formats it in its own commit, so the hook does not reformat a hot file mid-backlog.
      [if] the file is a .py, frontend source or shell script in this repo, new or formatted at HEAD [then ⛔️]
        it is rewritten to its formatter's output, exit 0
      [if] the file's HEAD version is unformatted (backlog) [then ⛔️] untouched, a notice on stderr, exit 0
      [if] the path is outside this checkout, missing, or of another type [then ⛔️] no-op, exit 0
      [if] the formatter fails (parse error, tool missing, wrong version) [then ⛔️] the error on stderr, exit 1
      [if] stdin is not a PostToolUse payload with tool_input.file_path [then ⛔️] the error on stderr, exit 1
Pinned by (private path).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.format_check_changed import REPO, ToolError, formatted_at_base, kind_of, unformatted, write

EXIT_FORMATTER_ERROR = 1


def _edited_path(payload: str) -> Path:
    try:
        data = json.loads(payload)
        return Path(data["tool_input"]["file_path"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"not a PostToolUse payload with tool_input.file_path: {exc!r}") from exc


def _committed(repo: Path, rel: str) -> bytes | None:
    proc = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{rel}"], capture_output=True, check=False)
    return proc.stdout if proc.returncode == 0 else None


def format_edited_file(repo: Path, file_path: Path) -> str:
    """What the hook did, as one line; raises ToolError when the formatter fails."""
    target = (file_path if file_path.is_absolute() else repo / file_path).resolve()
    if not target.is_relative_to(repo.resolve()) or not target.is_file():
        return f"[format-on-edit] skip {file_path}: not a file in {repo}"
    rel = target.relative_to(repo.resolve()).as_posix()
    kind = kind_of(rel, target.read_bytes()[:200])
    if kind is None:
        return f"[format-on-edit] skip {rel}: no ratified formatter owns this type"
    if rel not in unformatted(repo, kind, [rel]):  # also every path the formatter's own config excludes
        return f"[format-on-edit] ok {rel}"
    committed = _committed(repo, rel)
    if committed is not None and not formatted_at_base(repo, kind, rel, committed):
        return f"[format-on-edit] backlog {rel}: unformatted at HEAD, left for issue #4456 part 6"
    write(repo, kind, [rel])
    return f"[format-on-edit] formatted {rel} ({kind})"


def main(stdin: str | None = None, repo: Path = REPO) -> int:
    try:
        line = format_edited_file(repo, _edited_path(sys.stdin.read() if stdin is None else stdin))
    except (ToolError, ValueError) as exc:
        print(f"[format-on-edit] ERROR {exc}", file=sys.stderr)
        return EXIT_FORMATTER_ERROR
    print(line, file=sys.stderr if line.startswith("[format-on-edit] backlog") else sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
