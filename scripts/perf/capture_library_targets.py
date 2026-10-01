"""Pre-capture identity gate for the Gig-vs-Library capture (PERFMODE-14).

Extracted from `capture_library_mode.py` (quality-ratchet `file_size.over_limit_python`,
PR #4034): the module had grown past the 600-line gate as review rounds added
engine-pid-restart detection alongside the existing build-sha/frontend-mode
checks, and this gate is a single cohesive concern with its own imports.
"""

from __future__ import annotations

from pathlib import Path

from scripts.perf.capture_build_identity import (
    _REPO,
    _frontend_mode,
    _probe_engine,
    _read_engine_pid,
    _verify_capturing_checkout_at,
    _verify_frontend_build_version,
    _verify_served_build_sha,
)


def _verify_capture_targets(
    engine: str,
    frontend: str,
    app_build_sha: str,
    expected_engine_pid: int | None = None,
    repo_root: Path = _REPO,
) -> tuple[str | None, str | None, int | None]:
    """Every identity gate `main()` must pass before launching Playwright, in
    one place: engine reachable and clean at this sha, frontend a verifiable
    static build (never vite-dev) clean and matching this sha, and the
    capturing checkout itself clean with HEAD still at this sha. Returns
    `(reason, None, None)` on the first failure, or `(None, frontend_mode,
    engine_pid)` once all checks pass. Extracted from `main()` (quality-ratchet
    `ruff.complexity`, PR #4034): five review rounds of Codex P0/P1 findings
    each added one more gate here, and `main()`'s own branching grew with them.

    `expected_engine_pid`, when given, additionally requires the engine's OWN
    `os.getpid()` (via `_read_engine_pid`) to match it (Codex P1/BLOCKING, PR
    #4034, discussion_r4138422256): a same-sha, same-clean engine RESTART
    mid-capture (a redeploy, a crash and supervisor respawn) passes every
    check above again while genuinely swapping the process the Playwright
    sampler is attributing footprint/CPU to, and this is the only gate here
    that can tell the two apart. `main()` passes the pid the FIRST,
    pre-capture call returns back into the SECOND, post-capture call.

    `repo_root` defaults to the real checkout (what `main()` always wants) but
    is overridable for tests: a self-hosted CI runner's persistent workspace
    can carry ambient uncommitted noise unrelated to the code under test
    (build artifacts, caches from a prior job on the same reused workdir), so
    a test asserting "a clean checkout passes" needs a checkout it controls,
    not the real one. `.planning/debt/4034.md` logged this gap as a P2; it
    broke two fast-tier CI legs for real on this same PR (2026-09-29), which
    promotes the fix from debt to a required change.
    """
    probe_reason = _probe_engine(engine)
    if probe_reason is not None:
        return probe_reason, None, None
    engine_sha_reason = _verify_served_build_sha(engine, app_build_sha)
    if engine_sha_reason is not None:
        return engine_sha_reason, None, None
    engine_pid = _read_engine_pid(engine)
    if expected_engine_pid is not None and engine_pid != expected_engine_pid:
        return (
            f"engine pid changed mid-capture: was {expected_engine_pid}, now {engine_pid} "
            f"(both serving {app_build_sha} clean) -- the engine restarted during the "
            "capture, which can mix samples from two different process lifetimes into "
            "one measured row",
            None,
            None,
        )
    try:
        frontend_mode = _frontend_mode(frontend)
    except ConnectionError as exc:
        return str(exc), None, None
    if frontend_mode == "vite-dev":
        # Codex P1/BLOCKING, PR #4034, discussion_r4132371694 and
        # discussion_r4132371727: a vite-dev frontend has no non-proxied way
        # to state its own identity -- /_app/version.json 404s in dev mode
        # (SvelteKit only writes it for a static build), and /api/v1/build-info
        # is forwarded straight through to the engine, so it can never confirm
        # the frontend bundle itself. Refuse rather than silently trust an
        # unverifiable frontend.
        return (
            f"refusing to capture against a vite-dev frontend at {frontend}: its "
            "own build identity cannot be verified over HTTP (/_app/version.json "
            "is not served in dev mode, and /api/v1/build-info only reflects the "
            "proxied engine, not the frontend bundle) -- capture against a "
            "static build instead (npm run build, served as static files)",
            None,
            None,
        )
    frontend_sha_reason = _verify_frontend_build_version(frontend, app_build_sha)
    if frontend_sha_reason is not None:
        return frontend_sha_reason, None, None
    checkout_reason = _verify_capturing_checkout_at(app_build_sha, repo_root)
    if checkout_reason is not None:
        return checkout_reason, None, None
    return None, frontend_mode, engine_pid
