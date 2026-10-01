"""Build-identity verification for PERFMODE-14 captures.

Does the engine, the frontend, and the checkout DRIVING the capture actually
run the commit a ledger row is about to claim? Split out of
capture_library_mode.py (quality-ratchet `file_size.over_limit_python`, PR
#4034): these identity checks and that module's own scoring/ledger-writing
logic are different concerns that happened to grow in the same file across
five review rounds of Codex P0/P1 findings, each closing one more way a stale,
foreign, or dirty build could silently pass as the named commit.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_REPO = Path(__file__).resolve().parents[2]
_PROBE_TIMEOUT_S = 10.0
_VITE_DEV_MARKER = "/@vite/client"
# Matches svelte.config.js's kit.version.name suffix for an uncommitted tree
# (Codex P1/BLOCKING, PR #4034, discussion_r4132707456). Spelled once here and
# in svelte.config.js's own comment.
_FRONTEND_DIRTY_SUFFIX = "-dirty"


def _git_sha(repo_root: Path = _REPO) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _verify_capturing_checkout_clean(repo_root: Path = _REPO) -> str | None:
    """This process's OWN tree, not just the engine's and frontend's.

    Codex P1/BLOCKING, PR #4034, discussion_r4132814651: `_run_playwright_capture`
    runs `library-mode-perf-capture.spec.ts` and the process sampler straight
    out of THIS checkout, never through a served origin -- the engine-sha and
    frontend-version checks above verify the two DAEMONS, but say nothing
    about the harness that drives and samples them. An operator could edit
    the spec or sampler after starting clean, matching services, and both
    prior checks would still pass (the served shas are unchanged, and the
    served dirty flags were captured before the edit), letting modified
    harness logic emit `measured: true` rows attributed to the clean commit
    `app_build_sha` names.
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    if proc.stdout.strip() != "":
        return (
            f"the capturing checkout at {repo_root} is DIRTY (uncommitted changes "
            "were present) -- capture_library_mode.py, the Playwright spec, and "
            "the process sampler all run out of this tree, so a dirty checkout "
            "can emit rows attributed to a commit it does not actually run; "
            "commit or stash before capturing release evidence"
        )
    return None


def _verify_capturing_checkout_at(expected_sha: str, repo_root: Path = _REPO) -> str | None:
    """None when the capturing checkout is clean AND at `expected_sha`; otherwise why not.

    Sol P1/BLOCKING, PR #4540: clean is not enough. A checkout switched to
    ANOTHER clean commit mid-capture runs a different harness while the engine
    and frontend still serve `expected_sha`, so every served-identity check
    passes and the rows are attributed to the wrong harness.
    """
    dirty_reason = _verify_capturing_checkout_clean(repo_root)
    if dirty_reason is not None:
        return dirty_reason
    checkout_sha = _git_sha(repo_root)
    if checkout_sha != expected_sha:
        return (
            f"the capturing checkout at {repo_root} is at {checkout_sha}, not "
            f"{expected_sha}: the harness that ran is not the build the rows name"
        )
    return None


def _http_json(method: str, url: str, timeout_s: float = _PROBE_TIMEOUT_S) -> tuple[int, Any]:
    request = Request(url, headers={"Accept": "application/json"}, method=method)
    try:
        with urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw else None
            return response.status, payload
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        return exc.code, payload
    except URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc
    except OSError as exc:
        raise ConnectionError(f"{type(exc).__name__}: {exc}") from exc


def _probe_engine(engine: str, timeout_s: float = _PROBE_TIMEOUT_S) -> str | None:
    try:
        status, _payload = _http_json(
            "GET", f"{engine.rstrip('/')}/api/v1/health", timeout_s=timeout_s
        )
    except ConnectionError as exc:
        return f"engine unreachable: {exc}"
    if status != 200:
        return f"engine health returned HTTP {status}"
    return None


def _verify_served_build_sha(origin: str, expected_sha_full: str) -> str | None:
    """The engine's actual served identity, not just "is it up".

    Codex P1/BLOCKING, PR #4034, discussion_r4131404896: `--engine` can point
    at a stale service or another checkout entirely (this capture attaches
    with `PERFORMANCE_E2E_START_SERVERS=0`), while the ledger's
    `app_build_sha` came only from THIS process's own `git rev-parse HEAD` --
    a dirty or stale engine could be marked `measured: true` under a SHA it
    never actually ran. `/api/v1/build-info`'s `git_sha_full` is the SERVING
    PROCESS's own answer for what it is actually running
    (apps/engine_core/build_info.py). Returns None when it matches and is
    clean, or a reason string otherwise -- never silent.

    Engine-only: the frontend has its own, non-proxied check --
    `_verify_frontend_build_version` -- because this endpoint is served BY
    the engine and vite-dev's `/api` proxy (vite.config.ts) forwards a
    frontend-side call straight through to it, so calling this same function
    against `--frontend` verifies the engine a second time, not the frontend
    bundle (Codex P1/BLOCKING, discussion_r4132371694).
    """
    try:
        status, payload = _http_json("GET", f"{origin.rstrip('/')}/api/v1/build-info")
    except ConnectionError as exc:
        return f"build-info unreachable at {origin}: {exc}"
    if status != 200:
        return f"build-info at {origin} returned HTTP {status}"
    if not isinstance(payload, dict) or "git_sha_full" not in payload:
        return f"build-info response at {origin} missing git_sha_full: {payload!r}"
    served_sha = payload["git_sha_full"]
    if served_sha != expected_sha_full:
        return (
            f"{origin} is serving {served_sha}, not the capturing checkout's "
            f"{expected_sha_full} -- point --engine at this checkout "
            "or re-measure from the one it actually serves"
        )
    # Codex P1/BLOCKING, PR #4034, discussion_r4132371711: git_sha_full alone
    # says which commit, not whether the tree was clean at that commit --
    # build-info always carries git_dirty too (apps/engine_core/build_info.py,
    # BuildInfoOut.git_dirty: bool), and skipping it lets uncommitted engine
    # changes be measured and recorded as though they were the named commit.
    if "git_dirty" not in payload:
        return f"build-info response at {origin} missing git_dirty: {payload!r}"
    if payload["git_dirty"] is not False:
        return (
            f"{origin} is serving a DIRTY build of {expected_sha_full} "
            "(uncommitted changes were present) -- commit or stash before "
            "capturing release evidence"
        )
    return None


def _read_engine_pid(engine: str) -> int:
    """The engine's own `os.getpid()`, from `/api/v1/build-info`'s
    `BuildInfoOut.pid` (apps/engine_core/build_info.py).

    Codex P1/BLOCKING, PR #4034, discussion_r4138422256: `_verify_served_build_sha`
    proves WHICH commit is running, not that it is still the SAME PROCESS as a
    prior check -- a same-SHA engine restart mid-capture (a redeploy, a crash
    and supervisor respawn) passes the sha/dirty checks again while genuinely
    swapping the process the sampler is attributing footprint/CPU to. Callers
    pin this value from their first, pre-capture call and pass it back to
    `_verify_capture_targets` after the dwell to catch that.

    Raises rather than returning a reason string: this is only ever called
    once `_verify_served_build_sha` has already confirmed the origin answers
    build-info with the right sha, so a missing or non-integer pid at that
    point is a real defect in the engine's own response, not a normal refusal
    path a capture should recover from by printing a reason and exiting 1.
    """
    status, payload = _http_json("GET", f"{engine.rstrip('/')}/api/v1/build-info")
    if status != 200:
        raise RuntimeError(f"build-info at {engine} returned HTTP {status} while reading its pid")
    if not isinstance(payload, dict) or not isinstance(payload.get("pid"), int):
        # A malformed HTTP response body, not a Python argument type error;
        # RuntimeError matches every other engine-response defect this module
        # raises (see _verify_served_build_sha above).
        raise RuntimeError(  # noqa: TRY004
            f"build-info response at {engine} carries no usable pid: {payload!r}"
        )
    return payload["pid"]


def _verify_frontend_build_version(frontend: str, expected_sha_full: str) -> str | None:
    """The static frontend build's OWN served identity, proxy-free.

    Codex P1/BLOCKING, PR #4034, discussion_r4132371694: `/api/v1/build-info`
    is served BY THE ENGINE. vite-dev's own `/api` proxy (vite.config.ts)
    forwards a frontend-side request there transitively, so checking it from
    `--frontend` only ever re-verifies the engine and proves nothing about
    which frontend bundle is actually served -- a stale or dirty frontend
    checkout behind a correct engine still passes.

    `_app/version.json` is different: it is a plain STATIC file that
    adapter-static emits at `vite build` time (svelte.config.js's
    `kit.version.name`, set to the git sha), under no proxy rule in
    vite.config.ts at all. Confirmed live (Mon 29 Sep 2026): a running
    `vite dev` server answers `/_app/version.json` with 404 -- SvelteKit only
    writes that file as part of a static build, never in dev mode. So this
    check is meaningful for exactly the case that matters for release
    evidence (a static build) and correctly cannot be satisfied by a dev
    server pretending to be one; `main()` refuses a vite-dev frontend before
    ever calling this (Codex P1/BLOCKING, discussion_r4132371727).

    Codex P1/BLOCKING, PR #4034, discussion_r4132707456: the sha alone says
    which commit the frontend was built FROM, not whether the tree was clean
    at that commit -- unlike the engine's build-info, which always carries
    `git_dirty`. svelte.config.js now suffixes a dirty build's version with
    `-dirty`; a served version matching the expected sha plus that suffix is
    refused explicitly, by name, rather than falling through to the generic
    "wrong build" message below.
    """
    try:
        status, payload = _http_json("GET", f"{frontend.rstrip('/')}/_app/version.json")
    except ConnectionError as exc:
        return f"frontend version manifest unreachable at {frontend}: {exc}"
    if status != 200:
        return f"frontend version manifest at {frontend} returned HTTP {status}"
    if not isinstance(payload, dict) or "version" not in payload:
        return f"frontend version manifest at {frontend} missing 'version': {payload!r}"
    served_version = payload["version"]
    if served_version == f"{expected_sha_full}{_FRONTEND_DIRTY_SUFFIX}":
        return (
            f"{frontend} is serving a DIRTY frontend build of {expected_sha_full} "
            "(uncommitted changes were present at build time) -- rebuild from a "
            "clean tree before capturing release evidence"
        )
    if served_version != expected_sha_full:
        return (
            f"{frontend} is serving frontend build {served_version!r}, not the "
            f"capturing checkout's {expected_sha_full} -- rebuild the frontend "
            "from this checkout (npm run build) or point --frontend at the "
            "static build that matches"
        )
    return None


def _frontend_mode(frontend: str) -> str:
    """Which bundle the capture measures: the Vite dev server or a static build.

    The packaged app ships the static build. The dev server keeps HMR
    wrappers and per-signal debug labels alive, which on silver held about
    250 MB more renderer footprint in Library mode (Sat 26 Sep 2026), so a
    row must say which one it measured.
    """
    request = Request(f"{frontend.rstrip('/')}/", headers={"Accept": "text/html"})
    try:
        with urlopen(request, timeout=_PROBE_TIMEOUT_S) as response:
            html = response.read().decode("utf-8", errors="replace")
    except (URLError, OSError) as exc:
        raise ConnectionError(f"frontend unreachable: {exc}") from exc
    return "vite-dev" if _VITE_DEV_MARKER in html else "static-build"
