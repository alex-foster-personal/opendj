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
  [if] PATCH omits If-Match [then ⛔️] 428 without writing
  [if] PATCH presents a stale ledger ETag [then ⛔️] 409 without writing
  [if] two processes PATCH from one ETag [then] exactly one commits and the
       loser receives the winner's ETag in a 409 conflict
  [if] a process exits while holding the ledger lock [then] the next writer
       acquires the OS-released lock and commits
  [if] another host owns the write lock [then ⛔️] 503 without writing
"""
from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

if os.name == "nt":
    import msvcrt
else:
    import fcntl

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from ..backend import StateBackend
from ..deps import get_write_state

router = APIRouter(prefix="/progress", tags=["progress"])
logger = logging.getLogger(__name__)

REPO_ROOT: Path = Path(__file__).resolve().parents[4]
# Canonical route dependency. Core transactions take an explicit path so tests
# can exercise disposable production-format ledgers without replacing globals.
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
# A bounded batch keeps SHA provenance validation from monopolizing the
# ledger-wide write lock. Larger historical imports must be sent in batches.
MAX_COMMITS_APPEND: int = 100

BuildState = Literal["active", "idle", "blocked", "hanging"]

# WHERE a node can be iteratively built: cloud (Claude Code cloud / any remote
# sandbox) vs this Mac. cloud = the build+test loop never needs local-only
# resources; hybrid = cloud-able against fixtures with a final local verify;
# local = iteration itself needs hardware / the real library / a remote host.
# Full rationale + per-node reasons in
# .planning/rekordbox-parity/CLOUD-BUILDABILITY.md.
BUILDABLE_TIERS: tuple[str, ...] = ("cloud", "hybrid", "local")
TierLiteral = Literal["cloud", "hybrid", "local"]

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
    "buildable": "optional {tier, reason}; tier one of cloud|hybrid|local -- "
    "where the node's iterative build loop can run (see "
    ".planning/rekordbox-parity/CLOUD-BUILDABILITY.md)",
}
PATCH_RULES: list[str] = [
    "PATCH requires If-Match with the current ETag returned by GET /progress",
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
    "'buildable' {tier, reason} replaces the node's buildable classification "
    "wholesale; tier is one of cloud|hybrid|local",
    "every sha must match ^[0-9a-f]{7,40}$ and resolve via git cat-file -e",
    f"commits_append accepts at most {MAX_COMMITS_APPEND} commits per PATCH",
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


class BuildPatchIn(BaseModel):
    """Partial 'build' object; only supplied fields are merged (see PATCH_RULES).

    'updated' is never accepted from the client -- the server always stamps
    it on any build PATCH, so there is no ambiguity about who wrote it.
    """
    model_config = ConfigDict(extra="forbid")
    branch: str | None = None
    pr: str | None = None
    worktree: str | None = None
    stage: str | None = None
    state: BuildState | None = None


class LinksPatchIn(BaseModel):
    """Partial 'links' object; each supplied key REPLACES that key's list."""
    model_config = ConfigDict(extra="forbid")
    issues: list[str] | None = None
    specs: list[str] | None = None
    refs: list[str] | None = None


class BuildableIn(BaseModel):
    """Buildability classification; both fields required, replaces wholesale.

    tier says WHERE the node's iterative build loop can run (cloud/hybrid/
    local); reason is a one-line justification grounded in what the node
    touches. See .planning/rekordbox-parity/CLOUD-BUILDABILITY.md.
    """
    model_config = ConfigDict(extra="forbid")
    tier: TierLiteral
    reason: str


class NodePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: StatusLiteral | None = None
    note: str | None = None
    commits_append: list[CommitIn] | None = Field(
        default=None, max_length=MAX_COMMITS_APPEND,
    )
    tests_append: list[str] | None = None
    verified: VerifiedIn | None = None
    build: BuildPatchIn | None = None
    links: LinksPatchIn | None = None
    buildable: BuildableIn | None = None


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


def _etag_for_payload(payload: bytes) -> str:
    return f'"{hashlib.sha256(payload).hexdigest()}"'


def _load_tree_payload(payload: bytes) -> dict[str, Any]:
    tree = yaml.safe_load(payload.decode("utf-8"))
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
            buildable = node.get("buildable")
            if buildable is not None and buildable.get("tier") not in BUILDABLE_TIERS:
                raise _http_error(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "progress_file_invalid",
                    f"node {node['id']} has unknown buildable tier "
                    f"{buildable.get('tier')!r} (expected "
                    f"{'|'.join(BUILDABLE_TIERS)})",
                )
    return tree


def _read_tree_snapshot(progress_file: Path) -> tuple[dict[str, Any], str, str]:
    """Read, parse, and hash one immutable ledger payload."""
    if not progress_file.is_file():
        raise _http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "progress_file_missing",
            f"canonical ledger not found at {progress_file}",
        )
    payload = progress_file.read_bytes()
    return (
        _load_tree_payload(payload),
        _etag_for_payload(payload),
        payload.decode("utf-8"),
    )


def _header_comment_lines(text: str) -> list[str]:
    """Leading '#' comment block (the in-file schema doc), preserved on write."""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            lines.append(line)
        else:
            break
    return lines


def _dump_tree_atomic(
    progress_file: Path,
    tree: dict[str, Any],
    current_text: str,
) -> str:
    """Atomic write: tmp file in the same dir + os.replace.

    The leading comment block of the existing file (the schema doc) is
    re-emitted so PATCH rewrites never strip the documentation.
    """
    header = _header_comment_lines(current_text)
    body = yaml.safe_dump(
        tree, sort_keys=False, allow_unicode=True, default_flow_style=False,
    )
    rendered = ("\n".join(header) + "\n" if header else "") + body
    payload = rendered.encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(
        dir=str(progress_file.parent), prefix=".progress-tree.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, progress_file)
        if os.name != "nt":
            directory_fd = os.open(progress_file.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    return _etag_for_payload(payload)


@contextmanager
def _progress_file_lock(progress_file: Path) -> Iterator[None]:
    """Lock local threads, then processes, for one ledger transaction.

    The adjacent sidecar is persistent by design. OS descriptor cleanup
    releases the advisory lock even when a process exits abnormally.
    """
    with _WRITE_LOCK:
        lock_path = progress_file.with_name(f"{progress_file.name}.lock")
        with lock_path.open("a+b") as lock_handle:
            if os.name == "nt":
                lock_handle.seek(0, os.SEEK_END)
                if lock_handle.tell() == 0:
                    lock_handle.write(b"\0")
                    lock_handle.flush()
                    os.fsync(lock_handle.fileno())
                while True:
                    lock_handle.seek(0)
                    try:
                        msvcrt.locking(
                            lock_handle.fileno(),
                            msvcrt.LK_LOCK,
                            1,
                        )
                    except OSError as exc:
                        # LK_LOCK raises after ten one-second attempts. Retry
                        # only lock-contention failures so Windows matches the
                        # POSIX wait-until-acquired transaction contract.
                        if exc.errno not in {errno.EACCES, errno.EDEADLK}:
                            raise
                    else:
                        break
            else:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    lock_handle.seek(0)
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    git_env = os.environ.copy()
    git_env["GIT_NO_LAZY_FETCH"] = "1"
    return subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT),
        capture_output=True, text=True, check=False, env=git_env,
    )


def _file_git_provenance(progress_file: Path) -> dict[str, str | None]:
    """Last commit touching the ledger file; null fields when uncommitted.

    A ledger outside REPO_ROOT (tests use a tmp copy)
    has no git history by definition -> explicit nulls, not a git error.
    """
    try:
        rel = str(progress_file.resolve().relative_to(REPO_ROOT))
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
    return datetime.now(UTC).astimezone().isoformat(
        timespec="seconds",
    )


def _patch_progress_file(
    progress_file: Path,
    node_id: str,
    patch: NodePatch,
    if_match: str,
) -> tuple[NodePatchOut, str]:
    """Apply one complete interprocess compare-and-swap transaction."""
    if not patch.model_dump(exclude_none=True):
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "empty_patch",
            "provide at least one of status/note/commits_append/"
            "tests_append/verified/build/links/buildable",
        )
    alias_used = patch.status in _DEPRECATED_STATUS_ALIASES
    effective_status = _DEPRECATED_STATUS_ALIASES.get(patch.status, patch.status)

    with _progress_file_lock(progress_file):
        tree, current_etag, current_text = _read_tree_snapshot(progress_file)
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
                commit.model_dump() for commit in patch.commits_append
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
        if patch.buildable is not None:
            node["buildable"] = patch.buildable.model_dump()

        # Preserve the deprecation note even when the same PATCH sets note.
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

        # Building without a branch/PR is a visible nag, not a write block.
        if status_changing and effective_status == "building":
            build = node.get("build") or {}
            if not build.get("branch") and not build.get("pr"):
                _append_note(node, "building without branch/pr recorded")

        tree["meta"]["updated"] = _now_iso()
        new_etag = _dump_tree_atomic(progress_file, tree, current_text)
        output = NodePatchOut(node=node, meta_updated=tree["meta"]["updated"])
        return output, new_etag


# ----- routes -----------------------------------------------------------------

@router.get("", responses={200: {"headers": _ETAG_RESPONSE_HEADER}})
def get_progress(response: Response) -> dict[str, Any]:
    """Full parsed tree + ledger-file git provenance + per-area rollups."""
    tree, etag, _text = _read_tree_snapshot(PROGRESS_FILE)
    response.headers["ETag"] = etag
    rollups = {
        area["id"]: _rollup(area["nodes"]) for area in tree["areas"]
    }
    return {
        **tree,
        "file_git": _file_git_provenance(PROGRESS_FILE),
        "rollups": rollups,
    }


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
            "commits_append": "optional list of up to "
            f"{MAX_COMMITS_APPEND} {{sha, note}} entries",
            "tests_append": "optional list of strings",
            "verified": "optional {by, method}; date is stamped server-side; "
            "required in the same PATCH when status becomes user-finalized",
            "build": "optional partial {branch?, pr?, worktree?, stage?, "
            "state?}; merges into the existing build object, server stamps "
            "updated",
            "links": "optional partial {issues?, specs?, refs?}; each "
            "supplied key replaces that key's list in the existing object",
            "buildable": "optional {tier, reason}; tier one of "
            "cloud|hybrid|local; replaces the node's classification wholesale",
        },
        "tiers": list(BUILDABLE_TIERS),
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
    if_match: str | None = Header(None, alias="If-Match"),
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> NodePatchOut:
    """Guarded partial update of one node; atomic YAML rewrite, no git commit."""
    if if_match is None:
        raise _http_error(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "precondition_required",
            "PATCH /progress/nodes/{node_id} requires If-Match header",
        )
    output, etag = _patch_progress_file(
        PROGRESS_FILE,
        node_id,
        patch,
        if_match,
    )
    response.headers["ETag"] = etag
    return output
