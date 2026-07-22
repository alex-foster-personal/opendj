"""Progress-tree routes -- canonical feature ledger for parallel agent work.

Serves and mutates ``data/progress-tree.yaml`` (Claude workflows + Codex
agents share it). Git SHA provenance is the point: status changes must carry
commit SHAs that resolve in this repo, verified via ``git cat-file -e``.

The API never git-commits the file -- agents own their commits. Writes are
atomic (tmp + rename) and bump ``meta.updated``.

Status lifecycle (ordered): missing -> spiked -> building -> partial ->
built -> verified -> merged -> user-finalized. 'working' is a DEPRECATED
alias for 'built', coerced server-side with a logged deprecation note (grace
period for in-flight agent fleets; sunset tracked in
.planning/FANOUT-CONVENTIONS.md). 'user-finalized' additionally requires a
verified {by, method} block in the same PATCH -- it is the maintainer's personal QA
blessing, the top of the lifecycle.

Requirements (mini-PRD):
  ✔︎ ✅ GET /progress: parsed tree + file git provenance + per-area rollups
  ✔︎ ✅ PATCH /progress/nodes/{node_id}: guarded partial update, atomic write
  ✔︎ ✅ GET /progress/schema: agent-discoverable node schema + statuses
Acceptance:
  [if] GET with seed file [then] areas + rollups + meta round-trip intact
  [if] PATCH status change without commits_append (new status not
       missing/spiked) [then ⛔️] 422
  [if] PATCH status change to user-finalized without verified [then ⛔️] 422
  [if] PATCH sha malformed or unresolvable in repo [then ⛔️] 422
  [if] PATCH unknown node id [then ⛔️] 404
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/progress", tags=["progress"])
logger = logging.getLogger(__name__)

REPO_ROOT: Path = Path(__file__).resolve().parents[4]
# Module-level so tests can monkeypatch to a tmp copy of the seed file.
PROGRESS_FILE: Path = REPO_ROOT / "data" / "progress-tree.yaml"

STATUSES: tuple[str, ...] = (
    "missing", "spiked", "building", "partial", "built", "verified",
    "merged", "user-finalized",
)
StatusLiteral = Literal[
    "missing", "spiked", "building", "partial", "built", "verified",
    "merged", "user-finalized",
    "working",  # deprecated alias for 'built'; coerced server-side
]
# missing/spiked are exempt from the commits_append rule: they represent
# absence or investigation-only outcomes, so there is no code commit to cite.
_STATUSES_WITHOUT_COMMITS: frozenset[str] = frozenset({"missing", "spiked"})
# Backward-compat: PATCH status='working' coerces to 'built' (grace period
# for fleets mid-flight on the old lifecycle name); see FANOUT-CONVENTIONS.md.
_DEPRECATED_STATUS_ALIASES: dict[str, str] = {"working": "built"}

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_WRITE_LOCK = threading.Lock()

BuildState = Literal["active", "idle", "blocked", "hanging"]

NODE_SCHEMA: dict[str, str] = {
    "id": "kebab-case string, globally unique across all areas",
    "title": "string",
    "status": "one of: " + "|".join(STATUSES),
    "effort": "S|M|L",
    "reuse": "string naming the existing repo feature to build on, or null",
    "deps": "list of node ids this node depends on",
    "commits": "list of {sha, note}; sha must resolve in this repo",
    "tests": "list of strings (test files/ids covering this node)",
    "verified": "{by, date, method} or null; agents identify themselves in by",
    "notes": "string or null",
    "build": "optional {branch?, pr?, worktree?, stage?, state?, updated?}; "
    "state one of active|idle|blocked|hanging; updated is server-stamped",
    "links": "optional {issues?: [str], specs?: [str], refs?: [str]}",
}
PATCH_RULES: list[str] = [
    "status changes MUST include commits_append unless the new status is "
    "missing or spiked",
    "status 'user-finalized' additionally requires verified {by, method} in "
    "the same PATCH (422 without) -- the maintainer's personal QA blessing",
    "status 'working' is a deprecated alias for 'built': accepted, coerced "
    "server-side, and logged with a deprecation note (see "
    "FANOUT-CONVENTIONS.md for sunset)",
    "PATCHing status to 'building' without branch/pr in build (existing or "
    "supplied) appends a 'building without branch/pr recorded' note, not "
    "a 422 -- friction nag, not a block",
    "'build' partial merges into the existing build object; server stamps "
    "build.updated on any build PATCH",
    "'links' partial merges into the existing links object, per key",
    "every sha must match ^[0-9a-f]{7,40}$ and resolve via git cat-file -e",
    "the API bumps meta.updated but never git-commits; agents own their "
    "commits",
]


# ----- pydantic models (inline per router convention) -------------------------

class CommitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha: str
    note: str


class VerifiedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: str
    method: str


class BuildPatchIn(BaseModel):
    """Partial 'build' object; only supplied fields are merged (see PATCH_RULES).

    'updated' is never accepted from the client -- the server always stamps
    it on any build PATCH, so there is no ambiguity about who wrote it.
    """
    model_config = ConfigDict(extra="forbid")
    branch: Optional[str] = None
    pr: Optional[str] = None
    worktree: Optional[str] = None
    stage: Optional[str] = None
    state: Optional[BuildState] = None


class LinksPatchIn(BaseModel):
    """Partial 'links' object; each supplied key REPLACES that key's list."""
    model_config = ConfigDict(extra="forbid")
    issues: Optional[list[str]] = None
    specs: Optional[list[str]] = None
    refs: Optional[list[str]] = None


class NodePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[StatusLiteral] = None
    note: Optional[str] = None
    commits_append: Optional[list[CommitIn]] = None
    tests_append: Optional[list[str]] = None
    verified: Optional[VerifiedIn] = None
    build: Optional[BuildPatchIn] = None
    links: Optional[LinksPatchIn] = None


class NodePatchOut(BaseModel):
    node: dict[str, Any]
    meta_updated: str


# ----- helpers ----------------------------------------------------------------

def _http_error(code: int, error: str, message: str) -> HTTPException:
    """Match the repo-wide {detail: {error, message}} error body shape."""
    return HTTPException(code, detail={"error": error, "message": message})


def _append_note(node: dict[str, Any], segment: str) -> None:
    """Append a ' | '-joined note segment, never clobbering existing notes."""
    existing = node.get("notes")
    node["notes"] = f"{existing} | {segment}" if existing else segment


def _load_tree() -> dict[str, Any]:
    if not PROGRESS_FILE.is_file():
        raise _http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "progress_file_missing",
            f"canonical ledger not found at {PROGRESS_FILE}",
        )
    tree = yaml.safe_load(PROGRESS_FILE.read_text(encoding="utf-8"))
    if not isinstance(tree, dict) or "meta" not in tree or "areas" not in tree:
        raise _http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "progress_file_invalid",
            "progress-tree.yaml must have top-level meta + areas",
        )
    seen: set[str] = set()
    for area in tree["areas"]:
        for node in area["nodes"]:
            if node["id"] in seen:
                raise _http_error(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "progress_file_invalid",
                    f"duplicate node id: {node['id']}",
                )
            seen.add(node["id"])
            if node["status"] not in STATUSES:
                raise _http_error(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "progress_file_invalid",
                    f"node {node['id']} has unknown status {node['status']!r}",
                )
    return tree


def _header_comment_lines(text: str) -> list[str]:
    """Leading '#' comment block (the in-file schema doc), preserved on write."""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            lines.append(line)
        else:
            break
    return lines


def _dump_tree_atomic(tree: dict[str, Any]) -> None:
    """Atomic write: tmp file in the same dir + os.replace.

    The leading comment block of the existing file (the schema doc) is
    re-emitted so PATCH rewrites never strip the documentation.
    """
    header = _header_comment_lines(PROGRESS_FILE.read_text(encoding="utf-8"))
    body = yaml.safe_dump(
        tree, sort_keys=False, allow_unicode=True, default_flow_style=False,
    )
    fd, tmp_path = tempfile.mkstemp(
        dir=str(PROGRESS_FILE.parent), prefix=".progress-tree.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if header:
                fh.write("\n".join(header) + "\n")
            fh.write(body)
        os.replace(tmp_path, PROGRESS_FILE)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT),
        capture_output=True, text=True, check=False,
    )


def _file_git_provenance() -> dict[str, Optional[str]]:
    """Last commit touching the ledger file; null fields when uncommitted.

    A ledger outside REPO_ROOT (tests point PROGRESS_FILE at a tmp copy)
    has no git history by definition -> explicit nulls, not a git error.
    """
    try:
        rel = str(PROGRESS_FILE.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return {"last_sha": None, "last_author": None, "last_date": None}
    proc = _git("log", "-1", "--format=%H%x1f%an%x1f%aI", "--", rel)
    if proc.returncode != 0:
        raise _http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "git_error",
            f"git log failed: {proc.stderr.strip()}",
        )
    out = proc.stdout.strip()
    if not out:
        return {"last_sha": None, "last_author": None, "last_date": None}
    sha, author, date = out.split("\x1f")
    return {"last_sha": sha, "last_author": author, "last_date": date}


def _require_resolvable_sha(sha: str) -> None:
    if not _SHA_RE.match(sha):
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_sha",
            f"sha {sha!r} does not match ^[0-9a-f]{{7,40}}$",
        )
    if _git("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown_sha",
            f"sha {sha!r} does not resolve to a commit in this repo",
        )


def _find_node(tree: dict[str, Any], node_id: str) -> dict[str, Any]:
    for area in tree["areas"]:
        for node in area["nodes"]:
            if node["id"] == node_id:
                return node
    raise _http_error(
        status.HTTP_404_NOT_FOUND, "not_found",
        f"unknown progress node id: {node_id}",
    )


def _rollup(nodes: list[dict[str, Any]]) -> dict[str, int]:
    counts = {s: 0 for s in STATUSES}
    for node in nodes:
        counts[node["status"]] += 1
    return counts


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds",
    )


# ----- routes -----------------------------------------------------------------

@router.get("")
def get_progress() -> dict[str, Any]:
    """Full parsed tree + ledger-file git provenance + per-area rollups."""
    tree = _load_tree()
    rollups = {
        area["id"]: _rollup(area["nodes"]) for area in tree["areas"]
    }
    return {**tree, "file_git": _file_git_provenance(), "rollups": rollups}


@router.get("/schema")
def get_progress_schema() -> dict[str, Any]:
    """Agent-discoverable node schema, allowed statuses, and PATCH rules."""
    return {
        "statuses": list(STATUSES),
        "efforts": ["S", "M", "L"],
        "node": NODE_SCHEMA,
        "patch_rules": PATCH_RULES,
        "patch_body": {
            "status": "optional, one of statuses (plus deprecated alias "
            "'working' -> coerced to 'built')",
            "note": "optional string, replaces node.notes",
            "commits_append": "optional list of {sha, note}",
            "tests_append": "optional list of strings",
            "verified": "optional {by, method}; date is stamped server-side; "
            "required in the same PATCH when status becomes user-finalized",
            "build": "optional partial {branch?, pr?, worktree?, stage?, "
            "state?}; merges into the existing build object, server stamps "
            "updated",
            "links": "optional partial {issues?, specs?, refs?}; each "
            "supplied key replaces that key's list in the existing object",
        },
    }


@router.patch("/nodes/{node_id}", response_model=NodePatchOut)
def patch_progress_node(node_id: str, patch: NodePatch) -> NodePatchOut:
    """Guarded partial update of one node; atomic YAML rewrite, no git commit."""
    fields_set = patch.model_dump(exclude_none=True)
    if not fields_set:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "empty_patch",
            "provide at least one of status/note/commits_append/"
            "tests_append/verified/build/links",
        )
    for commit in patch.commits_append or []:
        _require_resolvable_sha(commit.sha)

    alias_used = patch.status in _DEPRECATED_STATUS_ALIASES
    effective_status = _DEPRECATED_STATUS_ALIASES.get(patch.status, patch.status)

    with _WRITE_LOCK:
        tree = _load_tree()
        node = _find_node(tree, node_id)

        status_changing = (
            effective_status is not None and effective_status != node["status"]
        )
        if status_changing:
            if (
                effective_status not in _STATUSES_WITHOUT_COMMITS
                and not patch.commits_append
            ):
                raise _http_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "status_change_needs_commits",
                    f"changing status {node['status']!r} -> "
                    f"{effective_status!r} requires commits_append (only "
                    "missing/spiked are exempt: they carry no code to cite)",
                )
            if effective_status == "user-finalized" and patch.verified is None:
                raise _http_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "user_finalized_needs_verified",
                    "changing status to user-finalized requires a verified "
                    "{by, method} block in the same PATCH -- it marks the maintainer's "
                    "personal QA blessing, the top of the status lifecycle",
                )
            node["status"] = effective_status

        if patch.note is not None:
            node["notes"] = patch.note
        if patch.commits_append:
            node.setdefault("commits", [])
            node["commits"].extend(
                c.model_dump() for c in patch.commits_append
            )
        if patch.tests_append:
            node.setdefault("tests", [])
            node["tests"].extend(patch.tests_append)
        if patch.verified is not None:
            node["verified"] = {
                "by": patch.verified.by,
                "date": _now_iso(),
                "method": patch.verified.method,
            }
        if patch.build is not None:
            node.setdefault("build", {})
            node["build"].update(patch.build.model_dump(exclude_none=True))
            node["build"]["updated"] = _now_iso()
        if patch.links is not None:
            node.setdefault("links", {})
            node["links"].update(patch.links.model_dump(exclude_none=True))

        # Deprecation note appended AFTER note/build handling so it survives
        # (and is never clobbered by) an explicit patch.note in the same call.
        if alias_used:
            _append_note(
                node,
                "DEPRECATED: PATCH sent status 'working', coerced to 'built' "
                "(alias sunset tracked in .planning/FANOUT-CONVENTIONS.md)",
            )
            logger.warning(
                "progress PATCH %s: deprecated status alias 'working' "
                "coerced to 'built'", node_id,
            )

        # Friction nag, not a block: building without a branch/pr recorded
        # is common early, but should stay visible in the ledger.
        if status_changing and effective_status == "building":
            build = node.get("build") or {}
            if not build.get("branch") and not build.get("pr"):
                _append_note(node, "building without branch/pr recorded")

        tree["meta"]["updated"] = _now_iso()
        _dump_tree_atomic(tree)

    return NodePatchOut(node=node, meta_updated=tree["meta"]["updated"])
