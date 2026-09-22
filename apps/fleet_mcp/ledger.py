"""The fan-out progress ledger (``data/progress-tree.yaml``) over HTTP.

``.planning/FANOUT-CONVENTIONS.md`` makes the ledger the LOCK: a feature is
claimed BEFORE it is built, by PATCHing ``/api/v1/progress/nodes/{id}`` to
``status: building`` with a ``build`` block. That PATCH requires an ``If-Match``
ETag, so doing it by hand is a read, a header copy and a retry -- exactly the
friction this connector exists to absorb.

The claim is lease-based: the server stamps ``build.updated`` and a claim goes
stale after :data:`LEASE_HOURS`. Taking over a LIVE claim is another agent's
work being overwritten, so it needs ``DISPATCH_MCP_ENABLE_TAKEOVER=1`` and a
note; a stale one does not.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

import httpx

from apps.fleet_mcp.config import (
    ENABLE_TAKEOVER_ENV,
    LEDGER_TIMEOUT_S,
    takeover_enabled,
)
from apps.fleet_mcp.runner import UNKNOWN

LEASE_HOURS = 3.0
_PROGRESS_PATH = "/api/v1/progress"

#: ``build.state`` values the progress route accepts (its own ``BuildState``
#: Literal, apps/webui/server/routes/progress.py). A fresh claim is "active".
#: This is NOT the node's ``status``: "building" is a status and was the
#: default here, which the route rejected outright -- a defect the always-200
#: test transport hid exactly as it hid the missing commits_append (#3735).
BUILD_STATES: tuple[str, ...] = ("active", "idle", "blocked", "hanging")
DEFAULT_BUILD_STATE = "active"


class LedgerFailure(RuntimeError):
    """The ledger could not be read, or the write was refused."""

    def __init__(self, document: dict[str, Any]) -> None:
        super().__init__(document.get("message") or document.get("reason") or "ledger failure")
        self.document = document


def base_url() -> str:
    """Resolve this worktree's backend origin, or say why it is unknown.

    ``resolve_backend_port`` reads the port THIS worktree claimed (``just
    webui-ports``). It is imported lazily so that importing this module never
    depends on the webui package being importable.
    """
    try:
        from apps.webui.port_config import resolve_backend_port
    except ImportError as error:  # pragma: no cover - environment defect
        raise LedgerFailure(
            {
                "error": "ledger_unknown",
                "status": UNKNOWN,
                "reason": f"apps.webui.port_config is not importable: {error}",
            }
        ) from error
    try:
        port = resolve_backend_port(None)
    except Exception as error:  # port_config raises several types
        raise LedgerFailure(
            {
                "error": "ledger_unknown",
                "status": UNKNOWN,
                "reason": f"this worktree has no resolvable backend port: {error}",
                "remedy": "run `just webui-ports` in this worktree",
            }
        ) from error
    return f"http://127.0.0.1:{port}"


def _unreachable(origin: str, error: Exception) -> LedgerFailure:
    return LedgerFailure(
        {
            "error": "ledger_unknown",
            "status": UNKNOWN,
            "reason": f"no daemon answering at {origin}: {error}",
            "remedy": "start the backend (`just webui`) in this worktree",
        }
    )


def _client(existing: httpx.Client | None) -> httpx.Client:
    """Yield the caller's client, or a short-lived one this module owns.

    A caller-supplied client is NOT closed here: it belongs to the caller, and
    a context manager that closed it would break the second call through it.
    """
    if existing is not None:
        return contextlib.nullcontext(existing)  # type: ignore[return-value]
    return httpx.Client(timeout=LEDGER_TIMEOUT_S)


def _snapshot(client: httpx.Client, origin: str) -> tuple[dict[str, Any], str]:
    try:
        response = client.get(f"{origin}{_PROGRESS_PATH}")
    except httpx.TransportError as error:
        raise _unreachable(origin, error) from error
    if not response.is_success:
        raise LedgerFailure(
            {
                "error": "ledger_error",
                "status_code": response.status_code,
                "message": response.text[:500],
            }
        )
    return response.json(), response.headers.get("ETag", "")


def _nodes(tree: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        node
        for area in tree.get("areas", [])
        if isinstance(area, dict)
        for node in area.get("nodes", [])
        if isinstance(node, dict)
    ]


def _lease_age_hours(node: dict[str, Any]) -> float | None:
    """Hours since the server stamped ``build.updated``, or None if unstamped."""
    build = node.get("build")
    stamp = build.get("updated") if isinstance(build, dict) else None
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        updated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return (datetime.now(UTC) - updated).total_seconds() / 3600.0


def read(
    node_id: str | None = None,
    *,
    status: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Read the ledger: one node by id, or a status-filtered summary."""
    origin = base_url()
    with _client(client) as session:
        tree, etag = _snapshot(session, origin)
    nodes = _nodes(tree)
    if node_id is not None:
        for node in nodes:
            if node.get("id") == node_id:
                return {
                    "origin": origin,
                    "etag": etag,
                    "node": node,
                    "lease_age_hours": _lease_age_hours(node),
                }
        raise LedgerFailure(
            {"error": "node_not_found", "message": f"no ledger node with id {node_id!r}"}
        )
    if status is not None:
        nodes = [node for node in nodes if node.get("status") == status]
    return {
        "origin": origin,
        "etag": etag,
        "rollups": tree.get("rollups", {}),
        "count": len(nodes),
        "nodes": [
            {
                "id": node.get("id"),
                "title": node.get("title", ""),
                "status": node.get("status", ""),
                "build": node.get("build", {}),
                "lease_age_hours": _lease_age_hours(node),
            }
            for node in nodes
        ],
    }


def _claim_conflict(node_id: str, node: dict[str, Any], age: float | None) -> LedgerFailure:
    raw_build = node.get("build")
    build: dict[str, Any] = raw_build if isinstance(raw_build, dict) else {}
    return LedgerFailure(
        {
            "error": "claim_held",
            "message": (
                f"{node_id} is already building on branch "
                f"{build.get('branch', '<unrecorded>')} with a live lease "
                f"({age:.1f}h of {LEASE_HOURS:g}h)" if age is not None
                else f"{node_id} is already building and its lease is unstamped"
            ),
            "held_by": build,
            "lease_age_hours": age,
            "remedy": (
                f"take it over only if that agent is gone: set {ENABLE_TAKEOVER_ENV}=1 "
                "and pass a note saying why"
            ),
        }
    )


def _require_claim_inputs(
    branch: str, worktree: str | None, pr: str | None, commit_sha: str, state: str
) -> None:
    """Refuse a claim the progress route would reject, before sending it.

    [if] the claim names a branch, a place for the work and a commit to cite
    [then] it is worth sending, [else stop] with the reason here rather than a
    422 from the server.
    """
    if not branch.strip():
        raise ValueError("branch must not be empty: the claim records where the work lands")
    if worktree is None and pr is None:
        raise ValueError("pass worktree or pr: a claim must say where the work is")
    # The route refuses every status change into `building` that cites no
    # commit: only `missing` and `spiked` are exempt, because they carry no
    # code (apps/webui/server/routes/progress.py, _STATUSES_WITHOUT_COMMITS).
    # A claim without one is a guaranteed 422, so it fails here with the
    # reason instead of at the server (Codex P1, #3735).
    if not commit_sha.strip():
        raise ValueError(
            "commit_sha must not be empty: claiming a node moves its status, and the "
            "progress route requires a resolvable commit to cite for any status change "
            "except missing/spiked. Pass the SHA your branch starts from."
        )
    if state not in BUILD_STATES:
        raise ValueError(
            f"state must be one of {', '.join(BUILD_STATES)} (got {state!r}); "
            "build.state describes the WORK, not the node's status"
        )


def _claim_payload(
    *,
    branch: str,
    state: str,
    worktree: str | None,
    pr: str | None,
    note: str | None,
    commit_sha: str,
) -> dict[str, Any]:
    """The PATCH body the ledger-as-lock convention and the route both expect.

    ``commits_append`` is not optional decoration: the route rejects a status
    change into ``building`` without it. The SHA is the base the claimed work
    starts from, which is the provenance a claim can honestly cite before any
    of the work exists.
    """
    build: dict[str, Any] = {"branch": branch, "state": state}
    if worktree is not None:
        build["worktree"] = worktree
    if pr is not None:
        build["pr"] = pr
    payload: dict[str, Any] = {
        "status": "building",
        "build": build,
        "commits_append": [{"sha": commit_sha, "note": f"claimed for {branch}"}],
    }
    if note:
        payload["note"] = note
    return payload


def _held_live(node: dict[str, Any], age: float | None) -> bool:
    """Whether this node is building under a lease that has not lapsed.

    An UNSTAMPED lease counts as live. An unmeasured lease is not an expired
    one, and failing open here would make an unstamped claim stealable.
    """
    return node.get("status") == "building" and (age is None or age < LEASE_HOURS)


def _claimed_node(response: httpx.Response) -> dict[str, Any]:
    """The claim's result, or the failure its status code names."""
    if response.status_code == 412:
        raise LedgerFailure(
            {
                "error": "etag_stale",
                "message": "the ledger changed between the read and the claim",
                "remedy": "call this tool again; another agent wrote a node meanwhile",
            }
        )
    if not response.is_success:
        raise LedgerFailure(
            {
                "error": "ledger_error",
                "status_code": response.status_code,
                "message": response.text[:500],
            }
        )
    document = response.json()
    node = document.get("node", {})
    return node if isinstance(node, dict) else {}


def _find_node(tree: dict[str, Any], node_id: str) -> dict[str, Any]:
    for item in _nodes(tree):
        if item.get("id") == node_id:
            return item
    raise LedgerFailure(
        {"error": "node_not_found", "message": f"no ledger node with id {node_id!r}"}
    )


def claim(
    node_id: str,
    *,
    branch: str,
    commit_sha: str,
    worktree: str | None = None,
    pr: str | None = None,
    note: str | None = None,
    state: str = DEFAULT_BUILD_STATE,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Claim a ledger node as the ledger-as-lock convention requires.

    Refuses a node another agent holds under a live lease unless
    ``DISPATCH_MCP_ENABLE_TAKEOVER=1`` is set AND a note is supplied, because
    a silent takeover is the one outcome the lock exists to prevent.
    """
    _require_claim_inputs(branch, worktree, pr, commit_sha, state)
    origin = base_url()
    with _client(client) as session:
        tree, etag = _snapshot(session, origin)
        node = _find_node(tree, node_id)
        age = _lease_age_hours(node)
        live_claim = _held_live(node, age)
        if live_claim and not (takeover_enabled() and note):
            raise _claim_conflict(node_id, node, age)
        payload = _claim_payload(
            branch=branch,
            state=state,
            worktree=worktree,
            pr=pr,
            note=note,
            commit_sha=commit_sha,
        )
        try:
            response = session.patch(
                f"{origin}{_PROGRESS_PATH}/nodes/{node_id}",
                json=payload,
                headers={"If-Match": etag},
            )
        except httpx.TransportError as error:
            raise _unreachable(origin, error) from error

    return {
        "claimed": True,
        "node_id": node_id,
        "took_over": live_claim,
        "node": _claimed_node(response),
    }
