"""Prove a frontend change is exactly the pinned prettier's output of its parent (issue #4456, part 8).

`scripts/format_proof.py` proves Python by AST. Frontend sources have no stdlib parser here, so the claim
is stronger and narrower: each changed file at head must equal what the PINNED prettier makes of the base
file under the head commit's own `.prettierrc`, and formatting that output again must change nothing.
A hand edit is not prettier's output of anything it was made from, so it fails. This trusts prettier's own
printer to keep meaning, which its AST sanity check enforces upstream.

Requirements (mini-PRD):
  prove_prettier  ✔︎
    Each changed frontend source (ts, tsx, mts, cts, js, mjs, cjs, svelte under apps/webui/frontend) at
    head equals prettier(base) under head's `.prettierrc`, from the installed prettier whose version is
    the exact pin in head's package.json, and prettier(head) equals head.
      [if] a changed file differs from prettier's output of its base (any hand edit) [then ⛔️] exit 1
      [if] prettier's output is not stable on a second pass [then ⛔️] exit 1
      [if] the installed prettier is not the version head pins, or node or the helper is missing [then ⛔️] exit 2
      [if] head has no `.prettierrc`, it is not JSON, or it uses `overrides` [then ⛔️] exit 2 UNKNOWN
      [if] a side is not utf-8, or prettier cannot parse the base [then ⛔️] exit 2 UNKNOWN
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


class CFG:
    FRONTEND: str = "apps/webui/frontend"
    SUFFIXES: tuple[str, ...] = (".ts", ".tsx", ".mts", ".cts", ".js", ".mjs", ".cjs", ".svelte")
    HELPER: str = "scripts/format-proof-prettier.mjs"
    CONFIG: str = ".prettierrc"
    PACKAGE: str = "package.json"
    EXACT_VERSION_RE: re.Pattern[str] = re.compile(r"^\d+\.\d+\.\d+$")
    NODE_TIMEOUT_S: int = 900


@dataclass
class Verdict:
    exit_code: int
    files_checked: int = 0
    version: str = ""
    lines: list[str] = field(default_factory=list)


BlobReader = Callable[[str, str], bytes]


def is_prettier_path(path: str) -> bool:
    return path.startswith(CFG.FRONTEND + "/") and path.endswith(CFG.SUFFIXES)


# ----- helpers --------------------------------------------------------------


def _unknown(message: str) -> Verdict:
    return Verdict(2, lines=[f"[format-proof] UNKNOWN {message}"])


def _head_options(read_blob: BlobReader, head: str) -> dict[str, object] | Verdict:
    try:
        options = json.loads(read_blob(head, f"{CFG.FRONTEND}/{CFG.CONFIG}").decode("utf-8"))
    except (subprocess.CalledProcessError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _unknown(f"{CFG.FRONTEND}/{CFG.CONFIG} at {head} is missing or not JSON: {exc}")
    if not isinstance(options, dict) or "overrides" in options:
        return _unknown(f"{CFG.FRONTEND}/{CFG.CONFIG} at {head} is not a flat options object (overrides unsupported)")
    return options


def _head_pin(read_blob: BlobReader, head: str) -> str | Verdict:
    try:
        package = json.loads(read_blob(head, f"{CFG.FRONTEND}/{CFG.PACKAGE}").decode("utf-8"))
    except (subprocess.CalledProcessError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _unknown(f"{CFG.FRONTEND}/{CFG.PACKAGE} at {head} is missing or not JSON: {exc}")
    pin = str(package.get("devDependencies", {}).get("prettier", ""))
    if not CFG.EXACT_VERSION_RE.match(pin):
        return _unknown(f"prettier is not pinned to an exact version at {head}: {pin!r}")
    return pin


def _run_helper(frontend_dir: Path, options: dict[str, object], files: list[dict[str, str]]) -> dict | Verdict:
    node = shutil.which("node")
    helper = frontend_dir / CFG.HELPER
    if node is None:
        return _unknown("node is not on PATH, so prettier's output cannot be computed")
    if not helper.is_file() or not (frontend_dir / "node_modules" / "prettier").exists():
        return _unknown(f"{helper} or the frontend's node_modules/prettier is missing (run pnpm install)")
    proc = subprocess.run(
        [node, str(helper)],
        cwd=frontend_dir,
        input=json.dumps({"options": options, "files": files}),
        capture_output=True,
        text=True,
        timeout=CFG.NODE_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        return _unknown(f"prettier helper exited {proc.returncode}: {proc.stderr.strip()[-400:]}")
    return json.loads(proc.stdout)


# ----- proof ----------------------------------------------------------------


def prove_prettier(read_blob: BlobReader, base: str, head: str, paths: list[str], frontend_dir: Path) -> Verdict:
    """Each path at head must be prettier(base) and a fixed point of prettier; read_blob(rev, path) gives bytes."""
    options = _head_options(read_blob, head)
    if isinstance(options, Verdict):
        return options
    pin = _head_pin(read_blob, head)
    if isinstance(pin, Verdict):
        return pin
    files: list[dict[str, str]] = []
    heads: dict[str, str] = {}
    for path in paths:
        try:
            source = read_blob(base, path).decode("utf-8")
            heads[path] = read_blob(head, path).decode("utf-8")
        except subprocess.CalledProcessError as exc:
            return _unknown(f"{path}: git show exited {exc.returncode}, this clone cannot read a side")
        except UnicodeDecodeError as exc:
            return _unknown(f"{path} is not utf-8: {exc}")
        files.append({"path": path.removeprefix(CFG.FRONTEND + "/"), "source": source})
    answer = _run_helper(frontend_dir, options, files)
    if isinstance(answer, Verdict):
        return answer
    if answer["version"] != pin:
        return _unknown(f"installed prettier {answer['version']} is not the {pin} that {head} pins")
    verdict = Verdict(0, files_checked=len(paths), version=pin)
    for item in answer["results"]:
        path = f"{CFG.FRONTEND}/{item['path']}"
        if "error" in item:
            return _unknown(f"{path}: prettier cannot format the base side: {item['error'][:300]}")
        if item["formatted"] != heads[path]:
            verdict.exit_code = 1
            verdict.lines.append(f"[format-proof] FAIL {path}: not prettier@{pin}'s output of the base file")
        elif item["reformatted"] != item["formatted"]:
            verdict.exit_code = 1
            verdict.lines.append(f"[format-proof] FAIL {path}: prettier@{pin} is not idempotent on it")
    return verdict
