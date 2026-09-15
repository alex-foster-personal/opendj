"""GET /api/v1/admin/ports.

Regression lines:
- if the endpoint stops returning backend + frontend + api_proxy_target then broken
- if an unreserved worktree 200s instead of 503 then broken (silent empty section)
- if claim_ports is invoked from the GET handler then broken (registry rewrite)
- if a build with no Git worktree raw-500s instead of naming the reason then broken (issue #2786)
"""
from __future__ import annotations

import pytest

from apps.webui.port_config import PortConfigError, WebuiPorts, WorktreeUnavailableError
from apps.webui.server.routes import worktree_ports


def test_worktree_ports_happy_path(client, monkeypatch):
    ports = WebuiPorts(8680, 9400)
    monkeypatch.setattr(worktree_ports, "show_ports", lambda: ports)
    r = client.get("/api/v1/admin/ports")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"backend", "frontend", "api_proxy_target"}
    assert body["backend"] == 8680
    assert body["frontend"] == 9400
    assert body["api_proxy_target"] == "http://127.0.0.1:8680"


def test_unreserved_worktree_is_loud(client, monkeypatch):
    def _raise():
        raise PortConfigError("port pair 8680/9400 is not reserved by this worktree")

    monkeypatch.setattr(worktree_ports, "show_ports", _raise)
    r = client.get("/api/v1/admin/ports")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "worktree_ports_unreserved"
    assert "not reserved" in detail["message"]


def test_no_git_worktree_is_503_not_a_raw_500(client, monkeypatch):
    """An installed build (no .git to resolve) must 503 with a named code, not
    raw-500 uvicorn's generic unhandled-exception handler (issue #2786)."""

    def _raise():
        raise WorktreeUnavailableError(
            "this process is not running inside a Git worktree (e.g. an "
            "installed build), so no worktree port pair can be resolved"
        )

    monkeypatch.setattr(worktree_ports, "show_ports", _raise)
    r = client.get("/api/v1/admin/ports")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "worktree_ports_no_worktree"
    assert "Git worktree" in detail["message"]


pytestmark = pytest.mark.rb_parity
