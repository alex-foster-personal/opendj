"""CI venv provisioning makes zero package-index requests once the cache is warm.

Issue #4252: `uv pip install --exact --upgrade` revalidated every package's
index page on every job (150 requests to pypi.org/simple per pytest shard on a
warm cache, measured Mon 28 Sep 2026), so a slow PyPI turned trunk red.
`scripts/ci_venv.sh --lock <pylock.toml>` syncs from wheel URLs and hashes
instead. These tests run the real script and the real uv against a local PEP
503 index that logs every request, so the count is measured, not inferred, and
no test here touches the internet.

Requirements:
- ✔︎ With a warm cache, `ci_venv.sh --lock` sends zero requests to the index,
  for a fresh venv and for a reused one.
- ✔︎ With a warm cache it also succeeds under UV_OFFLINE=1.
- ✔︎ A reused venv ends up exactly the locked set: a package another job left
  behind is removed.
- ✔︎ The legacy `--upgrade` install DOES hit the index on a warm cache
  (positive control: the request log can fire).
- ✔︎ A cold cache never resolves: it fetches only the locked wheel files by
  URL and hash, and says so loudly with a CACHE_MISS annotation.
- ✔︎ Under UV_OFFLINE=1 a cold cache fails loud instead of fetching.

Acceptance tests:
- [if] provisioning revalidates index pages on a warm cache [then ⛔️] the
  zero-request test fails with the logged paths.
- [if] a stray package survives provisioning in a reused venv [then ⛔️] the
  exactness test fails naming it.
- [if] the request log cannot see uv's requests [then ⛔️] the positive
  control fails, so a zero count cannot come from a deaf instrument.
- [if] `--lock` names a missing file [then ⛔️] provisioning exits nonzero.
- [if] a cold cache reads an index page, or fetches without the CACHE_MISS
  warning [then ⛔️] the cold-fetch test fails.
- [if] the CACHE_MISS warning fires on a warm cache [then ⛔️] the warm test
  fails (overshoot control: the warning must mean something).
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.ci_lock import COMPILE_ARGS

REPO_ROOT = Path(__file__).resolve().parents[2]
VENV_SCRIPT = REPO_ROOT / "scripts" / "ci_venv.sh"
PYTHON = "3.11"
# PyPI's own cache headers, so uv's HTTP cache behaves as it does against pypi.org.
SIMPLE_CACHE_CONTROL = "max-age=600, public"
FILE_CACHE_CONTROL = "max-age=365000000, immutable, public"
# alpha depends on beta, so the lock carries a transitive pin; stray is never locked.
DISTRIBUTIONS = {"alpha": ["beta"], "beta": [], "stray": []}
# The annotation title ci_venv.sh emits when the warm-cache sync cannot complete.
CACHE_MISS = "CI venv cache miss"

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")


# -----------------------------------------------------------------------------
# a local PEP 503 index that records every request
# -----------------------------------------------------------------------------
def _wheel(name: str, requires: list[str]) -> bytes:
    """A minimal valid py3-none-any wheel with a correct RECORD."""
    dist_info = f"{name}-1.0.dist-info"
    files = {
        f"{name}/__init__.py": b"",
        f"{dist_info}/METADATA": "\n".join(
            ["Metadata-Version: 2.1", f"Name: {name}", "Version: 1.0"]
            + [f"Requires-Dist: {req}" for req in requires]
        ).encode()
        + b"\n",
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = (
        "".join(
            f"{path},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}\n"
            for path, data in files.items()
        )
        + f"{dist_info}/RECORD,,\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in {**files, f"{dist_info}/RECORD": record.encode()}.items():
            archive.writestr(path, data)
    return buffer.getvalue()


@dataclass
class Index:
    url: str
    requests: list[str] = field(default_factory=list)


@pytest.fixture
def index() -> Iterator[Index]:
    wheels = {
        f"{name}-1.0-py3-none-any.whl": _wheel(name, deps) for name, deps in DISTRIBUTIONS.items()
    }
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._respond(with_body=True)

        def do_HEAD(self) -> None:
            self._respond(with_body=False)

        def _respond(self, *, with_body: bool) -> None:
            seen.append(f"{self.command} {self.path}")
            self.with_body = with_body
            parts = self.path.strip("/").split("/")
            if parts[0] == "simple" and len(parts) == 2 and parts[1] in DISTRIBUTIONS:
                links = "".join(
                    f'<a href="/files/{f}#sha256={hashlib.sha256(w).hexdigest()}">{f}</a>'
                    for f, w in wheels.items()
                    if f.startswith(f"{parts[1]}-")
                )
                self._send(
                    f"<html><body>{links}</body></html>".encode(), "text/html", SIMPLE_CACHE_CONTROL
                )
            elif parts[0] == "files" and len(parts) == 2 and parts[1] in wheels:
                self._send(wheels[parts[1]], "application/octet-stream", FILE_CACHE_CONTROL)
            else:
                self.send_error(404)

        def _send(self, body: bytes, content_type: str, cache_control: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.with_body:
                self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Index(url=f"http://127.0.0.1:{server.server_address[1]}", requests=seen)
    finally:
        server.shutdown()
        server.server_close()


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------
@dataclass
class Job:
    """One CI workspace plus the uv environment its steps run under."""

    workdir: Path
    env: dict[str, str]

    @property
    def python(self) -> Path:
        return self.workdir / ".venv" / "bin" / "python"


@pytest.fixture
def job(tmp_path: Path, index: Index) -> Job:
    home = tmp_path / "home"
    home.mkdir()
    workdir = tmp_path / "ws"
    workdir.mkdir()
    interpreter_dir = Path(sys.executable).resolve().parent
    uv_dir = Path(shutil.which("uv") or "").resolve().parent
    env = {
        "PATH": f"/usr/bin:/bin:/usr/sbin:/sbin:{interpreter_dir}:{uv_dir}",
        "HOME": str(home),
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_NO_CONFIG": "1",
        "UV_DEFAULT_INDEX": f"{index.url}/simple",
    }
    (workdir / "requirements.in").write_text("alpha\n", encoding="utf-8")
    fixture_job = Job(workdir=workdir, env=env)
    _ok(
        _run(
            fixture_job,
            "uv",
            "pip",
            "compile",
            *COMPILE_ARGS,
            "-o",
            "pylock.test.toml",
            "requirements.in",
        )
    )
    return fixture_job


def _run(
    job: Job, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args),
        cwd=job.workdir,
        env=job.env | (extra_env or {}),
        capture_output=True,
        text=True,
        check=False,
    )


def _provision(
    job: Job, *extra: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return _run(job, "bash", str(VENV_SCRIPT), PYTHON, *extra, extra_env=extra_env)


def _legacy_install(job: Job) -> subprocess.CompletedProcess:
    """The pre-#4252 form every CI job ran."""
    return _run(
        job,
        "uv",
        "pip",
        "install",
        "--exact",
        "--upgrade",
        "--python",
        str(job.python),
        "-r",
        "requirements.in",
    )


def _installed(job: Job) -> set[str]:
    code = (
        "import importlib.metadata as m; "
        "print('\\n'.join(sorted(d.metadata['Name'] for d in m.distributions())))"
    )
    # cwd is the job's workspace: `-c` puts cwd on sys.path, so the repo's egg-info would count.
    out = subprocess.run(
        [str(job.python), "-c", code], cwd=job.workdir, capture_output=True, text=True, check=True
    )
    return set(out.stdout.split())


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


# -----------------------------------------------------------------------------
# acceptance
# -----------------------------------------------------------------------------
def test_warm_lock_provisioning_sends_zero_index_requests(job: Job, index: Index) -> None:
    _ok(_provision(job, "--lock", "pylock.test.toml"))  # cold: warms the cache
    assert index.requests, "control: the cold fill must fetch wheels from the index"

    index.requests.clear()
    shutil.rmtree(job.workdir / ".venv")
    fresh = _provision(job, "--lock", "pylock.test.toml")  # fresh venv, warm cache
    reused = _provision(job, "--lock", "pylock.test.toml")  # reused venv, warm cache

    _ok(fresh)
    _ok(reused)
    assert index.requests == [], f"warm provisioning contacted the index: {index.requests}"
    assert _installed(job) == {"alpha", "beta"}
    for result in (fresh, reused):
        assert CACHE_MISS not in result.stdout + result.stderr, "warm cache reported a miss"


def test_cold_lock_provisioning_fetches_pinned_files_without_resolving(
    job: Job, index: Index
) -> None:
    index.requests.clear()  # the fixture's compile read index pages; provisioning must not

    result = _provision(job, "--lock", "pylock.test.toml")

    _ok(result)
    assert index.requests, "control: a cold cache must fetch the locked wheels"
    resolving = [r for r in index.requests if not r.startswith("GET /files/")]
    assert not resolving, f"cold provisioning read more than locked files: {resolving}"
    assert f"::warning title={CACHE_MISS}" in result.stderr, result.stderr
    assert _installed(job) == {"alpha", "beta"}


def test_cold_cache_under_uv_offline_fails_loud(job: Job, index: Index) -> None:
    index.requests.clear()

    result = _provision(job, "--lock", "pylock.test.toml", extra_env={"UV_OFFLINE": "1"})

    assert result.returncode != 0, "a cold cache with the network forbidden must fail"
    assert CACHE_MISS in result.stderr, result.stderr
    assert index.requests == [], f"UV_OFFLINE=1 still contacted the index: {index.requests}"


def test_warm_lock_provisioning_succeeds_offline(job: Job) -> None:
    _ok(_provision(job, "--lock", "pylock.test.toml"))
    shutil.rmtree(job.workdir / ".venv")

    _ok(_provision(job, "--lock", "pylock.test.toml", extra_env={"UV_OFFLINE": "1"}))

    assert _installed(job) == {"alpha", "beta"}


def test_lock_provisioning_removes_packages_outside_the_lock(job: Job) -> None:
    _ok(_provision(job, "--lock", "pylock.test.toml"))
    _ok(_run(job, "uv", "pip", "install", "--python", str(job.python), "stray"))
    assert "stray" in _installed(job), "control: the pollution step must land"

    result = _provision(job, "--lock", "pylock.test.toml", extra_env={"UV_OFFLINE": "1"})

    _ok(result)
    assert "[venv] reusing .venv" in result.stdout, "must exercise the reused-venv path"
    assert _installed(job) == {"alpha", "beta"}, "a package outside the lock survived"


def test_legacy_upgrade_install_hits_the_index_even_when_warm(job: Job, index: Index) -> None:
    """Positive control: the request log can see uv, so zero above is a measurement."""
    _ok(_provision(job))
    _ok(_legacy_install(job))
    index.requests.clear()

    _ok(_legacy_install(job))

    assert any(line.startswith("GET /simple/") for line in index.requests), index.requests


def test_missing_lock_fails_provisioning(job: Job) -> None:
    result = _provision(job, "--lock", "pylock.absent.toml")
    assert result.returncode != 0
    assert "pylock.absent.toml" in result.stderr


def test_requirements_flag_is_gone() -> None:
    """The --upgrade path is removed, not left beside the lock path as an option."""
    code = [
        line
        for line in VENV_SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert code, "control: the script has code lines to inspect"
    offenders = [line for line in code if "--upgrade" in line or "--requirements" in line]
    assert not offenders, offenders
    assert os.access(VENV_SCRIPT, os.X_OK)
