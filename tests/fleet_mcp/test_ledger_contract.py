"""``ledger.claim`` against the REAL progress route, not a stand-in transport.

The rest of the ledger suite substitutes a transport that answers 200, which is
the right shape for testing THIS module's branching but says nothing about
whether the server would accept the payload. It did not: the route refuses every
status change into ``building`` that cites no commit, so the advertised
claim-before-building operation could not claim anything (Codex P1 on #3735).

A mock cannot answer a question about a contract it is standing in for, so these
tests drive the actual FastAPI app over ASGI.

[if] the claim sent here is one the real route would reject [then] this fails, [else stop]

Regression one-liners:
  - if a well-formed claim is not ACCEPTED by the real route then broken
  - if the pre-fix payload (no commits_append) is not refused 422 by that same
    route then this file has stopped being able to see the bug it exists for
  - if a claim with no commit_sha reaches the network at all then broken
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from apps.fleet_mcp import ledger
from apps.webui.server.app import create_app
from apps.webui.server.routes import progress as progress_module

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_FILE = REPO_ROOT / "data" / "progress-tree.yaml"
ORIGIN = "http://127.0.0.1"
NODE_ID = "usb-export"

pytestmark = [pytest.mark.requirement("AGENT-15")]


def _head_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class _SyncASGITransport(httpx.BaseTransport):
    """Drive an ASGI app from a SYNC ``httpx.Client``.

    ``ledger.claim`` is synchronous and takes an ``httpx.Client``, while
    ``httpx.ASGITransport`` only implements the async half, and Starlette's
    ``TestClient`` is an ``httpx2.Client`` rather than an ``httpx`` one. This
    bridge is the narrowest thing that lets the real app answer the real code
    path; it adds no behavior of its own, which is what keeps it from becoming
    the stand-in these tests exist to avoid.
    """

    def __init__(self, app: object) -> None:
        self._inner = httpx.ASGITransport(app=app)  # type: ignore[arg-type]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def _call() -> httpx.Response:
            response = await self._inner.handle_async_request(request)
            body = b"".join([chunk async for chunk in response.stream])  # type: ignore[union-attr]
            await response.aclose()
            return httpx.Response(
                response.status_code, headers=response.headers, content=body
            )

        return asyncio.run(_call())


@pytest.fixture
def real_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[httpx.Client]:
    """The real progress app over ASGI, against a tmp copy of the seed ledger."""
    copy = tmp_path / "progress-tree.yaml"
    shutil.copy(SEED_FILE, copy)
    monkeypatch.setattr(progress_module, "PROGRESS_FILE", copy)
    monkeypatch.setattr(ledger, "base_url", lambda: ORIGIN)
    app = create_app(mount_frontend=False)
    with httpx.Client(
        transport=_SyncASGITransport(app), base_url=ORIGIN, timeout=30.0
    ) as client:
        yield client


def test_a_well_formed_claim_is_accepted_by_the_real_route(
    real_route: httpx.Client,
) -> None:
    """[if] the tool claims an unclaimed node [then] the REAL route accepts it, [else stop]

    The presence of the good thing. Every other ledger test answers 200 from a
    stand-in, which cannot say whether the server would have taken the payload.
    """
    result = ledger.claim(
        NODE_ID,
        branch="af--dispatch-contract",
        commit_sha=_head_sha(),
        worktree="../music-dj-tools-wt-dispatch",
        client=real_route,
    )
    assert result["claimed"] is True
    assert result["node"]["status"] == "building"
    assert result["node"]["build"]["branch"] == "af--dispatch-contract"
    # The cited commit is what made the status change legal; prove it landed
    # rather than trusting the 200.
    assert result["node"]["commits"][-1]["sha"] == _head_sha()


def test_the_pre_fix_payload_is_refused_by_that_same_route(
    real_route: httpx.Client,
) -> None:
    """[if] a status change cites no commit [then] the route refuses it 422, [else stop]

    Negative control. If this ever goes green the route has stopped enforcing
    the contract, and the test above would pass for a payload that proves
    nothing -- so this is what keeps this file able to see the bug it exists for.
    """
    snapshot = real_route.get(f"{ORIGIN}/api/v1/progress")
    etag = snapshot.headers["ETag"]
    response = real_route.patch(
        f"{ORIGIN}/api/v1/progress/nodes/{NODE_ID}",
        json={"status": "building", "build": {"branch": "af--no-commits"}},
        headers={"If-Match": etag},
    )
    assert response.status_code == 422, response.text
    assert "status_change_needs_commits" in response.text


def test_a_claim_without_a_commit_sha_never_reaches_the_network(
    real_route: httpx.Client,
) -> None:
    """[if] a claim names no commit [then] it is refused before any request, [else stop]

    Refused locally with the reason rather than sent for the route to 422.
    """
    with pytest.raises(ValueError, match="commit_sha must not be empty"):
        ledger.claim(
            NODE_ID,
            branch="af--dispatch-contract",
            commit_sha="   ",
            worktree="../wt",
            client=real_route,
        )
