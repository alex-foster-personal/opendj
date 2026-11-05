"""The install-token CLI against the REAL gateway Worker.

The Worker runs under ``wrangler dev --local`` (workerd plus a local D1 in a
temp dir), and its upstream is a real loopback HTTP server replaying the same
recorded OpenAI-compatible frames the Worker's own vitest suite uses. A chat
goes through the engine's ``resolve_upstream()`` headers, so the frozen engine
wire contract (Bearer install token, X-Title, HTTP-Referer, base + ``/chat/completions``)
is exercised end to end rather than restated.

[if] a minted token cannot stream through the Worker, or a revoked one still can [then] fail, [else stop].
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from apps.ai_gateway_admin.cli import ADMIN_TOKEN_ENV, ADMIN_URL_ENV, EXIT_REFUSED, EXIT_UNCONFIGURED, main
from apps.engine_core.assistant.upstream import GATEWAY_URL_ENV, INSTALL_TOKEN_ENV, OPENROUTER_KEY_ENV, resolve_upstream
from tests.waits import start_uvicorn_in_thread, wait_for_external_state

pytestmark = pytest.mark.requirement("AISIDE-7892h")

WORKER_DIR = Path(__file__).resolve().parents[2] / "apps" / "ai_gateway_worker"
WRANGLER_JS = WORKER_DIR / "node_modules" / "wrangler" / "bin" / "wrangler.js"
FRAMES: bytes = (WORKER_DIR / "test" / "fixtures" / "openrouter-stream.sse").read_bytes()
FIXTURE_USAGE: dict[str, Any] = next(
    json.loads(line[5:])["usage"] for line in FRAMES.decode("utf-8").splitlines() if '"usage"' in line
)
ADMIN_TOKEN = "pytest-admin-token"
AIG_TOKEN = "pytest-aig-token"
WORKER_START_GUARD_S = 120.0
FLASH = "google/gemini-3.7-flash"


# ----- the real Worker and its real upstream ------------------------------
@dataclass
class Gateway:
    base_url: str
    seen: list[dict[str, Any]] = field(default_factory=list)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wrangler_argv(node: str) -> list[str]:
    return [node, str(WRANGLER_JS)]


def _upstream_app(seen: list[dict[str, Any]]) -> FastAPI:
    app = FastAPI()

    @app.post("/chat/completions")
    async def chat_completions(request: Request) -> Response:
        seen.append({"headers": dict(request.headers), "body": await request.json()})
        return StreamingResponse(iter([FRAMES]), media_type="text/event-stream")

    return app


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    parent = psutil.Process(proc.pid)
    family = [*parent.children(recursive=True), parent]
    for p in family:
        p.terminate()
    _, alive = psutil.wait_procs(family, timeout=10)
    for p in alive:
        p.kill()


def _worker_answers(base_url: str, proc: subprocess.Popen[bytes], log: Path) -> bool:
    if proc.poll() is not None:
        raise AssertionError(f"wrangler dev exited {proc.returncode}:\n{log.read_text('utf-8', 'replace')}")
    try:
        return httpx.get(f"{base_url}/", timeout=2).status_code == 404
    except httpx.TransportError:
        return False


@pytest.fixture(scope="module")
def gateway(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Gateway]:
    node = shutil.which("node")
    if not node or not WRANGLER_JS.is_file():
        pytest.skip(
            "UNAVAILABLE: the Workers runtime (wrangler dev / workerd) needs Node and "
            "`pnpm install` in apps/ai_gateway_worker"
        )
    seen: list[dict[str, Any]] = []
    config = uvicorn.Config(_upstream_app(seen), host="127.0.0.1", port=0, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the recorded upstream")
    upstream = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"

    scratch = tmp_path_factory.mktemp("ai_gateway")
    env = {**os.environ, "CI": "1", "WRANGLER_SEND_METRICS": "false"}
    wrangler = _wrangler_argv(str(node))
    persist = ["--persist-to", str(scratch / "state")]
    subprocess.run(
        [*wrangler, "d1", "migrations", "apply", "DB", "--local", *persist],
        cwd=WORKER_DIR,
        env=env,
        check=True,
        capture_output=True,
        timeout=WORKER_START_GUARD_S,
    )
    port, log = _free_port(), scratch / "wrangler-dev.log"
    with log.open("wb") as sink:
        proc = subprocess.Popen(
            [
                *wrangler,
                "dev",
                "--local",
                "--ip",
                "127.0.0.1",
                "--port",
                str(port),
                "--inspector-port",
                str(_free_port()),
                *persist,
                "--var",
                f"ADMIN_TOKEN:{ADMIN_TOKEN}",
                "--var",
                f"CF_AIG_TOKEN:{AIG_TOKEN}",
                "--var",
                f"UPSTREAM_BASE_URL:{upstream}",
            ],
            cwd=WORKER_DIR,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_for_external_state(
            lambda: _worker_answers(base_url, proc, log), what="wrangler dev serving", guard_s=WORKER_START_GUARD_S
        )
        yield Gateway(base_url=base_url, seen=seen)
    finally:
        _kill_tree(proc)
        server.should_exit = True
        thread.join(timeout=15.0)


# ----- helpers -------------------------------------------------------------
@pytest.fixture
def admin_env(gateway: Gateway, monkeypatch: pytest.MonkeyPatch) -> Gateway:
    monkeypatch.setenv(ADMIN_URL_ENV, gateway.base_url)
    monkeypatch.setenv(ADMIN_TOKEN_ENV, ADMIN_TOKEN)
    return gateway


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any]:
    """Run the CLI in-process; return its exit code and parsed stdout (or stderr on refusal)."""
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, json.loads(out if code == 0 else err)


def _engine_chat(base_url: str, token: str, monkeypatch: pytest.MonkeyPatch) -> httpx.Response:
    """One streamed chat exactly as the engine would send it, via its own upstream resolution."""
    monkeypatch.delenv(OPENROUTER_KEY_ENV, raising=False)
    monkeypatch.setenv(GATEWAY_URL_ENV, f"{base_url}/v1")
    monkeypatch.setenv(INSTALL_TOKEN_ENV, token)
    upstream = resolve_upstream()
    body = {"model": FLASH, "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    with httpx.stream(
        "POST", f"{upstream.base_url}/chat/completions", headers=upstream.headers(), json=body, timeout=30
    ) as response:
        response.read()
    return response


def _without_token(minted: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in minted.items() if k != "token"} | {"revoked_at": None}


# ----- tests ---------------------------------------------------------------
def test_minted_token_streams_and_usage_reports_the_metered_cost(
    admin_env: Gateway, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, minted = _cli(capsys, "mint", "--label", "pytest")
    assert code == 0 and minted["plan"] == "free" and minted["label"] == "pytest"

    response = _engine_chat(admin_env.base_url, minted["token"], monkeypatch)
    assert response.status_code == 200
    assert response.content == FRAMES

    forwarded = admin_env.seen[-1]
    assert forwarded["headers"]["cf-aig-authorization"] == f"Bearer {AIG_TOKEN}"
    assert forwarded["headers"]["x-title"] == "Open DJ"
    assert "authorization" not in forwarded["headers"]
    assert forwarded["body"]["usage"] == {"include": True}

    def metered() -> bool:
        days = _cli(capsys, "usage", minted["install_id"], "--days", "1")[1]["days"]
        return bool(days) and days[0]["requests"] == 1

    wait_for_external_state(metered, what="the Worker's waitUntil metering write", guard_s=30)
    day = _cli(capsys, "usage", minted["install_id"])[1]["days"][0]
    assert day["cost_usd"] == pytest.approx(FIXTURE_USAGE["cost"])
    assert (day["prompt_tokens"], day["completion_tokens"], day["unmetered"]) == (
        FIXTURE_USAGE["prompt_tokens"],
        FIXTURE_USAGE["completion_tokens"],
        0,
    )


def test_revoked_token_is_refused_by_the_worker(
    admin_env: Gateway, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    minted = _cli(capsys, "mint")[1]
    code, revoked = _cli(capsys, "revoke", minted["install_id"])
    assert code == 0 and revoked["revoked_at"]
    response = _engine_chat(admin_env.base_url, minted["token"], monkeypatch)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "gateway_token_invalid"


def test_set_plan_moves_the_install_and_refuses_an_unknown_plan(
    admin_env: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    minted = _cli(capsys, "mint")[1]
    assert _cli(capsys, "set-plan", minted["install_id"], "pro") == (0, {**_without_token(minted), "plan": "pro"})
    code, refused = _cli(capsys, "set-plan", minted["install_id"], "gold")
    assert code == EXIT_REFUSED
    assert (refused["status"], refused["detail"]["code"]) == (400, "gateway_unknown_plan")


def test_wrong_admin_token_is_refused(
    admin_env: Gateway, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "not-the-admin-token")
    code, refused = _cli(capsys, "mint")
    assert code == EXIT_REFUSED
    assert (refused["status"], refused["detail"]["code"]) == (401, "gateway_admin_unauthorized")


def test_unconfigured_cli_exits_without_calling(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ADMIN_URL_ENV, raising=False)
    monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
    assert main(["usage", "inst_x"]) == EXIT_UNCONFIGURED
    assert ADMIN_URL_ENV in capsys.readouterr().err
