"""Progress-tree routes -- canonical feature ledger for parallel agent work.

Serves and mutates ``data/progress-tree.yaml`` (Claude workflows + Codex
agents share it). Git SHA provenance is the point: status changes must carry
commit SHAs that resolve in this repo, verified via ``git cat-file -e``.

The API never git-commits the file -- agents own their commits. Writes are
atomic (tmp + rename) and bump ``meta.updated``.

Requirements (mini-PRD):
  ✔︎ ✅ GET /progress: parsed tree + file git provenance + per-area rollups
  ✔︎ ✅ PATCH /progress/nodes/{node_id}: guarded partial update, atomic write
  ✔︎ ✅ GET /progress/schema: agent-discoverable node schema + statuses
Acceptance:
  [if] GET with seed file [then] areas + rollups + meta round-trip intact
  [if] PATCH status change without commits_append (new status not
       missing/spiked) [then ⛔️] 422
  [if] PATCH sha malformed or unresolvable in repo [then ⛔️] 422
  [if] PATCH unknown node id [then ⛔️] 404
  [if] PATCH omits If-Match [then ⛔️] 428 without writing
  [if] PATCH presents a stale ledger ETag [then ⛔️] 409 without writing
  [if] another host owns the write lock [then ⛔️] 503 without writing
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, model_validator

from ..backend import StateBackend
from ..deps import get_write_state

router = APIRouter(prefix="/progress", tags=["progress"])

REPO_ROOT: Path = Path(__file__).resolve().parents[4]
# Module-level so tests can monkeypatch to a tmp copy of the seed file.
PROGRESS_FILE: Path = REPO_ROOT / "data" / "progress-tree.yaml"

STATUSES: tuple[str, ...] = (
    "missing", "spiked", "building", "partial", "working", "verified",
)
StatusLiteral = Literal[
    "missing", "spiked", "building", "partial", "working", "verified",
]
# missing/spiked are exempt from the commits_append rule: they represent
# absence or investigation-only outcomes, so there is no code commit to cite.
_STATUSES_WITHOUT_COMMITS: frozenset[str] = frozenset({"missing", "spiked"})

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_WRITE_LOCK = threading.Lock()

NODE_SCHEMA: dict[str, Any] = {
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
    "codex": {
        "safe": "true only after Windows/headless execution and path ownership are bounded",
        "rank": "positive integer; lower is more gating",
        "windows_ready": "boolean",
        "rationale": "non-empty string explaining why Codex should own the node",
        "owned_paths": "non-empty list of exclusive path globs",
        "avoid_paths": "non-empty list of shared or Mac-owned path globs",
        "claim": "{state: available|claimed, owner: string|null}",
    },
}
PATCH_RULES: list[str] = [
    "PATCH requires If-Match with the current ETag returned by GET /progress",
    "status changes MUST include commits_append unless the new status is "
    "missing or spiked",
    "every sha must match ^[0-9a-f]{7,40}$ and resolve via git cat-file -e",
    "the API bumps meta.updated but never git-commits; agents own their "
    "commits",
]

_ETAG_RESPONSE_HEADER: dict[str, dict[str, Any]] = {
    "ETag": {
        "description": "Strong validator for the exact progress ledger bytes",
        "schema": {"type": "string"},
    },
}
_PATCH_RESPONSES: dict[int, dict[str, Any]] = {
    200: {"headers": _ETAG_RESPONSE_HEADER},
    409: {
        "description": "If-Match is stale; refresh the ledger before retrying",
        "headers": _ETAG_RESPONSE_HEADER,
    },
    428: {"description": "If-Match header is required for every progress write"},
    503: {"description": "Writes are disabled because the cloud lock is unavailable or held by a peer"},
}


# ----- pydantic models (inline per router convention) -------------------------

class CommitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha: str
    note: str


class VerifiedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: str
    method: str


class CodexClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["available", "claimed"]
    owner: Optional[str] = None

    @model_validator(mode="after")
    def _state_matches_owner(self) -> "CodexClaimIn":
        owner = self.owner.strip() if self.owner is not None else None
        if self.state == "claimed" and not owner:
            raise ValueError("claimed Codex work requires a non-empty owner")
        if self.state == "available" and owner is not None:
            raise ValueError("available Codex work cannot have an owner")
        self.owner = owner
        return self


class NodePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[StatusLiteral] = None
    note: Optional[str] = None
    commits_append: Optional[list[CommitIn]] = None
    tests_append: Optional[list[str]] = None
    verified: Optional[VerifiedIn] = None
    codex_claim: Optional[CodexClaimIn] = None


class NodePatchOut(BaseModel):
    node: dict[str, Any]
    meta_updated: str


# ----- helpers ----------------------------------------------------------------

def _http_error(code: int, error: str, message: str) -> HTTPException:
    """Match the repo-wide {detail: {error, message}} error body shape."""
    return HTTPException(code, detail={"error": error, "message": message})


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
    codex_ranks: dict[int, str] = {}
    codex_owned_paths: list[tuple[str, bool, str]] = []
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
            codex = node.get("codex")
            if codex is not None:
                required = {
                    "safe", "rank", "windows_ready", "rationale",
                    "owned_paths", "avoid_paths", "claim",
                }
                if not isinstance(codex, dict) or set(codex) != required:
                    raise _http_error(
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                        "progress_file_invalid",
                        f"node {node['id']} has invalid codex assignment keys",
                    )
                if (
                    codex["safe"] is not True
                    or not isinstance(codex["rank"], int)
                    or isinstance(codex["rank"], bool)
                    or codex["rank"] < 1
                    or codex["windows_ready"] is not True
                    or not isinstance(codex["rationale"], str)
                    or not codex["rationale"].strip()
                    or not isinstance(codex["owned_paths"], list)
                    or not codex["owned_paths"]
                    or not all(isinstance(path, str) and path for path in codex["owned_paths"])
                    or not isinstance(codex["avoid_paths"], list)
                    or not codex["avoid_paths"]
                    or not all(isinstance(path, str) and path for path in codex["avoid_paths"])
                    or not isinstance(codex["claim"], dict)
                    or set(codex["claim"]) != {"state", "owner"}
                    or codex["claim"]["state"] not in {"available", "claimed"}
                    or (
                        codex["claim"]["state"] == "available"
                        and codex["claim"]["owner"] is not None
                    )
                    or (
                        codex["claim"]["state"] == "claimed"
                        and (
                            not isinstance(codex["claim"]["owner"], str)
                            or not codex["claim"]["owner"].strip()
                        )
                    )
                ):
                    raise _http_error(
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                        "progress_file_invalid",
                        f"node {node['id']} has an unsafe codex assignment",
                    )
                previous_node_id = codex_ranks.get(codex["rank"])
                if previous_node_id is not None:
                    raise _http_error(
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                        "progress_file_invalid",
                        f"Codex rank {codex['rank']} is shared by "
                        f"{previous_node_id} and {node['id']}",
                    )
                codex_ranks[codex["rank"]] = node["id"]
                for owned_path in codex["owned_paths"]:
                    normalized = owned_path.replace("\\", "/").strip("/")
                    recursive = normalized.endswith("/**") or owned_path.endswith("/")
                    scope = normalized.removesuffix("/**").rstrip("/")
                    for other_scope, other_recursive, other_node_id in codex_owned_paths:
                        overlaps = scope == other_scope or (
                            recursive and other_scope.startswith(scope + "/")
                        ) or (
                            other_recursive and scope.startswith(other_scope + "/")
                        )
                        if overlaps:
                            raise _http_error(
                                status.HTTP_500_INTERNAL_SERVER_ERROR,
                                "progress_file_invalid",
                                f"Codex owned path {owned_path!r} overlaps "
                                f"{other_node_id} and {node['id']}",
                            )
                    codex_owned_paths.append((scope, recursive, node["id"]))
    return tree


def _file_etag() -> str:
    """Strong validator for the exact canonical-ledger bytes on disk."""
    digest = hashlib.sha256(PROGRESS_FILE.read_bytes()).hexdigest()
    return f'"{digest}"'


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

@router.get("", responses={200: {"headers": _ETAG_RESPONSE_HEADER}})
def get_progress(response: Response) -> dict[str, Any]:
    """Full parsed tree + ledger-file git provenance + per-area rollups."""
    tree = _load_tree()
    response.headers["ETag"] = _file_etag()
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
            "status": "optional, one of statuses",
            "note": "optional string, replaces node.notes",
            "commits_append": "optional list of {sha, note}",
            "tests_append": "optional list of strings",
            "verified": "optional {by, method}; date is stamped server-side",
            "codex_claim": "optional {state: available|claimed, owner: string|null}",
        },
    }


@router.patch(
    "/nodes/{node_id}",
    response_model=NodePatchOut,
    responses=_PATCH_RESPONSES,
)
def patch_progress_node(
    node_id: str,
    patch: NodePatch,
    response: Response,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    _backend: StateBackend = Depends(get_write_state),
) -> NodePatchOut:
    """Guarded partial update of one node; atomic YAML rewrite, no git commit."""
    if if_match is None:
        raise _http_error(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "precondition_required",
            "PATCH /progress/nodes/{node_id} requires If-Match header",
        )
    fields_set = patch.model_dump(exclude_none=True)
    if not fields_set:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "empty_patch",
            "provide at least one of status/note/commits_append/"
            "tests_append/verified/codex_claim",
        )
    with _WRITE_LOCK:
        current_etag = _file_etag()
        if if_match != current_etag:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "conflict",
                    "message": "If-Match does not match the current progress ledger ETag",
                    "etag": current_etag,
                },
                headers={"ETag": current_etag},
            )
        for commit in patch.commits_append or []:
            _require_resolvable_sha(commit.sha)

        tree = _load_tree()
        node = _find_node(tree, node_id)

        if patch.status is not None and patch.status != node["status"]:
            if (
                patch.status not in _STATUSES_WITHOUT_COMMITS
                and not patch.commits_append
            ):
                raise _http_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "status_change_needs_commits",
                    f"changing status {node['status']!r} -> {patch.status!r} "
                    "requires commits_append (only missing/spiked are exempt: "
                    "they carry no code to cite)",
                )
            node["status"] = patch.status
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
        if patch.codex_claim is not None:
            if "codex" not in node:
                raise _http_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "codex_not_safe",
                    f"node {node_id!r} is not marked codex safe",
                )
            current_claim = node["codex"]["claim"]
            requested_claim = patch.codex_claim.model_dump()
            if (
                current_claim["state"] == "claimed"
                and requested_claim != current_claim
            ):
                raise _http_error(
                    status.HTTP_409_CONFLICT,
                    "claim_conflict",
                    f"node {node_id!r} is already claimed by "
                    f"{current_claim['owner']!r}; coordinator reassignment "
                    "must update the ledger explicitly",
                )
            node["codex"]["claim"] = requested_claim

        tree["meta"]["updated"] = _now_iso()
        _dump_tree_atomic(tree)
        response.headers["ETag"] = _file_etag()

    return NodePatchOut(node=node, meta_updated=tree["meta"]["updated"])
