"""Real partial-clone regression for `_measure_owners_at_base` (issue #4426).

job 110140841841 UNKNOWN'd base_compare on PR #4426: a self-hosted runner's
persistent workspace was a partial (promisor) clone, `git worktree add` at
the merge base needed a blob the initial checkout never fetched, and the
lazy promisor fetch that followed had no credentials (`persist-credentials:
false` in ci.yml) --  "could not read Username for 'https://github.com'".
#3459/#3464 hit the same promisor class in the hotspot `git log` and dodged
it with `--no-renames`; a checkout has no such dodge, since materializing
files is the entire point, so `scripts/quality_gate.py::_origin_auth_env`
supplies a per-invocation credential instead.

This runs a REAL local partial clone against a REAL (if tiny) authenticated
git-over-HTTP server -- no mocked git output anywhere -- so a passing test
proves the fix actually authenticates a real lazy fetch, not merely that some
code path was exercised. The body lives here rather than in
tests/test_quality_gate.py (already 1245 lines, over the 600-line split
gate); imported there so both `pytest tests/test_quality_gate.py` and a
bare `pytest tests/quality_gate_base_compare_partial_clone.py` collect it.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import quality_gate as qg

_TOKEN = "test-partial-clone-token"  # fixture value, not a real credential


class _GatedGitHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Proxies real `git http-backend` CGI output, gated on a bearer token.

    Everything below the auth check is genuine git smart-HTTP behavior --
    the same binary and protocol a real GitHub fetch uses -- so a client
    fetch against this server is as real as the CI failure it reproduces.
    """

    def _serve(self) -> None:
        required = f"bearer {self.server.required_token}"  # type: ignore[attr-defined]
        if self.headers.get("Authorization", "").lower() != required.lower():
            body = b"authentication required"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        length = int(self.headers.get("Content-Length") or 0)
        request_body = self.rfile.read(length) if length else b""
        path, _, query = self.path.partition("?")
        env = {
            **os.environ,
            "GIT_HTTP_EXPORT_ALL": "1",
            "GIT_PROJECT_ROOT": str(self.server.repo_root),  # type: ignore[attr-defined]
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REQUEST_METHOD": self.command,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(length),
            "REMOTE_ADDR": self.client_address[0],
            "SERVER_PROTOCOL": "HTTP/1.1",
            "GATEWAY_INTERFACE": "CGI/1.1",
        }
        proc = subprocess.run(
            ["git", "http-backend"], input=request_body, env=env,
            capture_output=True, check=False,
        )
        header_blob, _, resp_body = proc.stdout.partition(b"\r\n\r\n")
        status = 200
        out_headers: list[tuple[str, str]] = []
        for line in header_blob.split(b"\r\n"):
            if not line:
                continue
            key, _, value = line.partition(b": ")
            key_s, value_s = key.decode(), value.decode()
            if key_s.lower() == "status":
                status = int(value_s.split()[0])
            else:
                out_headers.append((key_s, value_s))
        self.send_response(status)
        for key_s, value_s in out_headers:
            self.send_header(key_s, value_s)
        self.end_headers()
        self.wfile.write(resp_body)

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def log_message(self, *args: object) -> None:
        pass  # keep pytest output quiet; failures surface via assertions


@contextmanager
def _auth_gated_origin(repo_root: Path, token: str) -> Iterator[tuple[str, str]]:
    """Serve `repo_root/origin.git` over real HTTP; yields (clone url, host prefix)."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GatedGitHTTPHandler)
    server.repo_root = repo_root  # type: ignore[attr-defined]
    server.required_token = token  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        prefix = f"http://127.0.0.1:{server.server_port}/"
        yield f"{prefix}origin.git", prefix
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _build_partial_clone(tmp_path: Path) -> tuple[Path, str, str]:
    """A real partial (`--filter=blob:none`) clone whose merge-base blob is missing.

    Two commits: C1 (track.txt="v1") is the merge base; C2 (track.txt="v2")
    is HEAD. The clone's own checkout fetches C2's blob (authenticated, like
    actions/checkout with a token). C1's blob never gets fetched -- same
    content-addressing reason the real PRs hit this: only files the branch
    actually changed are missing from a partial clone at the merge base.
    Returns (clone_dir, c1_sha, origin_http_prefix).
    """
    srv_root = tmp_path / "srv"
    seed = srv_root / "seed"
    seed.mkdir(parents=True)
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "track.txt").write_text("v1\n")
    _git(seed, "add", "track.txt")
    _git(seed, "commit", "-q", "-m", "c1")
    c1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=seed, capture_output=True, text=True, check=True
    ).stdout.strip()
    (seed / "track.txt").write_text("v2\n")
    _git(seed, "commit", "-q", "-am", "c2")

    bare = srv_root / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True, capture_output=True
    )
    _git(bare, "config", "uploadpack.allowfilter", "true")
    _git(bare, "config", "uploadpack.allowReachableSHA1InWant", "true")
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return srv_root, c1, ""


@pytest.mark.slow
def test_unauthenticated_worktree_add_reproduces_the_promisor_failure(
    tmp_path: Path,
) -> None:
    """Control: the auth gate genuinely fires, reproducing job 110140841841 for real.

    Without this, a later pass proves nothing -- the server might simply
    never have required credentials in the first place.
    """
    srv_root, c1, _ = _build_partial_clone(tmp_path)
    with _auth_gated_origin(srv_root, _TOKEN) as (origin_url, prefix):
        clone_dir = tmp_path / "clone"
        clone = subprocess.run(
            ["git", "-c", f"http.{prefix}.extraheader=AUTHORIZATION: bearer {_TOKEN}",
             "-c", "protocol.version=2",
             "clone", "-q", "--filter=blob:none", "--", origin_url, str(clone_dir)],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        assert clone.returncode == 0, clone.stderr
        assert "extraheader" not in (clone_dir / ".git" / "config").read_text(), (
            "the initial clone must not persist a credential -- persist-credentials: "
            "false leaves nothing behind for a later git call to inherit"
        )

        result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(tmp_path / "unauthed-base"), c1],
            cwd=clone_dir, capture_output=True, text=True, check=False,
        )

        assert result.returncode != 0, (
            "the server must actually refuse an unauthenticated lazy fetch, or this "
            "whole reproduction is testing nothing"
        )
        assert "could not fetch" in result.stderr and "promisor" in result.stderr, (
            f"expected the exact promisor-fetch failure shape from job 110140841841: "
            f"{result.stderr}"
        )


@pytest.mark.slow
def test_measure_owners_at_base_survives_partial_clone_with_gh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] GH_TOKEN is set and the runner is a partial clone [then] base_compare
    still measures the merge base instead of reporting UNKNOWN (#4426)."""
    srv_root, c1, _ = _build_partial_clone(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with _auth_gated_origin(srv_root, _TOKEN) as (origin_url, prefix):
        clone_dir = tmp_path / "clone"
        subprocess.run(
            ["git", "-c", f"http.{prefix}.extraheader=AUTHORIZATION: bearer {_TOKEN}",
             "-c", "protocol.version=2",
             "clone", "-q", "--filter=blob:none", "--", origin_url, str(clone_dir)],
            check=True, capture_output=True, text=True,
        )  # fmt: skip

        # No token: exactly today's pre-fix shape (a sanity check the real
        # failure survives the refactor that made `repo` injectable).
        monkeypatch.delenv("GH_TOKEN", raising=False)
        no_token_metrics, no_token_reason = qg._measure_owners_at_base(
            c1, [], repo=clone_dir
        )
        assert no_token_metrics is None
        assert "could not fetch" in no_token_reason or "promisor" in no_token_reason

        # With a token: the fix must make the SAME merge-base fetch succeed,
        # proven by reading back the real content checked out at C1 -- the
        # presence of the good thing, not merely the absence of an error.
        monkeypatch.setenv("GH_TOKEN", _TOKEN)
        captured: dict[str, object] = {}

        def _fake_run_in_base(
            cmd: list[str], base_dir: Path, ambient
        ) -> subprocess.CompletedProcess[str]:
            """Stand in for the nested gate run: this test is about the checkout
            succeeding with real content, not about running ruff/mypy/etc. on a
            two-file synthetic repo."""
            captured["track_txt"] = (base_dir / "track.txt").read_text()
            captured["token_leaked_to_base_run"] = (
                "GH_TOKEN" in ambient or "GITHUB_TOKEN" in ambient
            )
            (base_dir / "metrics.json").write_text(json.dumps({"fake": "owner"}))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(qg, "_run_in_base", _fake_run_in_base)

        metrics, reason = qg._measure_owners_at_base(c1, [], repo=clone_dir)

        assert metrics == {"fake": "owner"}, (
            f"the merge-base worktree add must succeed with GH_TOKEN present: {reason}"
        )
        assert captured["track_txt"] == "v1\n", (
            "the checked-out merge-base tree must hold the MERGE BASE's own content, "
            "not HEAD's -- proof the blob actually fetched rather than reusing "
            "something already on disk"
        )
        assert captured["token_leaked_to_base_run"] is False, (
            "the token must not reach the base's own committed gate subprocess"
        )
