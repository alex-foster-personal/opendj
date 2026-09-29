"""gh-version preflight (scripts/gh_version_guard.py), wired into
scripts/review_gh.py's `_gh` (which scripts/ci_wait.py imports directly, so
one guard covers both).

Fri 12 Sep 2026 onward, every scheduled `periodic-checks.yml` run died in its
first job: agentbox-15 (job 103871571683) ran gh 2.62.0, and this repo's
`--paginate --slurp` calls need gh 2.64.0. The failure rendered as the opaque
`GhError: unknown flag: --slurp`, naming neither the installed version nor
the fix. This module proves the NEXT stale-gh runner reports both instead.

Pure logic (`parse_gh_version`, `check_min_version`) is tested directly on
literal strings/tuples -- no `gh` invocation involved. AGENTS.md bars mocking
`_gh`/`gh` output, so the live path (`require_gh_min_version()` with no
override, and `_gh`'s own call to it) is proven by a real `gh` on PATH,
skipped when unavailable, mirroring
tests/scripts/test_review_coverage_pagination.py's `_gh_unavailable_reason`.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest

from scripts.gh_version_guard import (
    MIN_GH_VERSION,
    GhVersionError,
    check_min_version,
    parse_gh_version,
    require_gh_min_version,
)
from scripts.review_gh import _gh

# ----- parse_gh_version: pure, no `gh` involved -----------------------------


def test_parse_gh_version_reads_the_real_agentbox_15_output() -> None:
    """[if] fed the exact `gh --version` text agentbox-15 printed before the
    upgrade [then it parses to (2, 62, 0), not an error]."""
    raw = "gh version 2.62.0 (2024-11-14)\nhttps://github.com/cli/cli/releases/tag/v2.62.0\n"
    assert parse_gh_version(raw) == (2, 62, 0)


def test_parse_gh_version_reads_the_post_upgrade_output() -> None:
    raw = "gh version 2.100.0 (2026-09-03)\nhttps://github.com/cli/cli/releases/tag/v2.100.0\n"
    assert parse_gh_version(raw) == (2, 100, 0)


def test_parse_gh_version_rejects_unparseable_output() -> None:
    """[if] `gh --version` prints something this regex was never built for
    [then ⛔️] a clear GhVersionError, not a silent wrong tuple or an
    IndexError from an unchecked regex match."""
    with pytest.raises(GhVersionError, match="could not parse"):
        parse_gh_version("command not found: gh")


# ----- check_min_version: pure, mutate-the-guard in BOTH directions --------


def test_check_min_version_raises_for_the_observed_agentbox_15_version() -> None:
    """[if] the installed version is the one that actually broke
    periodic-checks.yml [then ⛔️] GhVersionError names BOTH the installed and
    the minimum version -- the whole point being a message better than
    `unknown flag: --slurp`."""
    with pytest.raises(GhVersionError) as exc_info:
        check_min_version((2, 62, 0), minimum=(2, 64, 0))
    message = str(exc_info.value)
    assert "2.62.0" in message
    assert "2.64.0" in message
    assert "--slurp" in message


def test_check_min_version_passes_at_exactly_the_floor() -> None:
    """[if] installed == minimum [then] no error -- a strict `<` comparison,
    not `<=`, is the off-by-one this boundary case catches."""
    check_min_version((2, 64, 0), minimum=(2, 64, 0))  # must not raise


def test_check_min_version_passes_for_the_post_upgrade_agentbox_version() -> None:
    """[if] installed is the version agentbox was actually upgraded to
    [then] no error -- the NEGATIVE control: this same function must not
    fail closed on a healthy runner."""
    check_min_version((2, 100, 0), minimum=MIN_GH_VERSION)  # must not raise


def test_check_min_version_default_minimum_matches_the_module_constant() -> None:
    """[if] a caller does not pass `minimum` [then] the default is exactly
    `MIN_GH_VERSION`, not a second, driftable copy of 2.64.0."""
    with pytest.raises(GhVersionError, match=r"2\.64\.0"):
        check_min_version((2, 63, 9))


def test_require_gh_min_version_accepts_an_explicit_installed_override() -> None:
    """[if] `installed` is passed explicitly [then] no `gh` subprocess is
    needed to prove the raise -- this is how every other test here exercises
    the guard without mocking `gh` itself."""
    with pytest.raises(GhVersionError, match=r"2\.62\.0"):
        require_gh_min_version(installed=(2, 62, 0))
    require_gh_min_version(installed=(2, 100, 0))  # must not raise


# ----- live path: real `gh --version`, no mock -----------------------------


def _gh_unavailable_reason() -> str | None:
    if shutil.which("gh") is None:
        return "UNAVAILABLE: gh CLI not on PATH"
    if subprocess.run(["gh", "--version"], capture_output=True, check=False).returncode != 0:
        return "UNAVAILABLE: gh --version failed"
    return None


_GH_UNAVAILABLE_REASON = _gh_unavailable_reason()


@pytest.mark.skipif(_GH_UNAVAILABLE_REASON is not None, reason=str(_GH_UNAVAILABLE_REASON))
def test_require_gh_min_version_passes_against_the_real_installed_gh() -> None:
    """Proves the live path (no override -> real `gh --version` subprocess)
    is actually wired, not just the pure comparison. This environment's `gh`
    is expected to be current; a failure here is a genuine finding about
    THIS machine's gh, not a fabricated result."""
    require_gh_min_version()  # must not raise


# ----- wiring: `_gh` calls the guard BEFORE shelling out to the real `gh` --


def test_gh_calls_the_version_guard_before_invoking_the_subprocess(monkeypatch) -> None:
    """Mutate the guard: if `_gh` ever stopped calling `require_gh_min_version`
    first, this test must go red. Patches this module's own control flow
    (not `gh`'s API output, which AGENTS.md protects) -- `subprocess.run` is
    replaced with a sentinel that fails the test if reached at all, so a
    passing run proves the guard short-circuits before any real `gh` call.
    """
    import scripts.review_gh as review_gh_module

    def _reject_version(*_args: object, **_kwargs: object) -> None:
        raise GhVersionError("gh 2.62.0 is older than the minimum 2.64.0 (test double)")

    def _subprocess_should_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("_gh must not shell out once the version guard has failed")

    monkeypatch.setattr(review_gh_module, "require_gh_min_version", _reject_version)
    monkeypatch.setattr(review_gh_module.subprocess, "run", _subprocess_should_not_run)

    with pytest.raises(GhVersionError, match="test double"):
        _gh(["pr", "view", "1"])


def test_gh_as_human_strips_gh_app_and_sets_allow_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] `_gh` is called with `as_human=True` and `GH_APP` is set [then] the
    child env drops `GH_APP` and sets `GH_ALLOW_HUMAN=1`, [else stop]."""
    import scripts.review_gh as review_gh_module

    monkeypatch.setenv("GH_APP", "fleet-installation")
    captured: dict[str, Any] = {}

    def _fake_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess:
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(review_gh_module, "require_gh_min_version", lambda: None)
    monkeypatch.setattr(review_gh_module.subprocess, "run", _fake_run)

    assert _gh(["pr", "view", "1"], as_human=True) == "ok"
    env = captured["env"]
    assert isinstance(env, dict)
    assert "GH_APP" not in env
    assert env.get("GH_ALLOW_HUMAN") == "1"


def test_gh_default_does_not_strip_gh_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] `_gh` is called without `as_human` and `GH_APP` is set [then] no
    `env` kwarg is passed (child inherits `os.environ` with `GH_APP` intact),
    [else stop]."""
    import scripts.review_gh as review_gh_module

    monkeypatch.setenv("GH_APP", "fleet-installation")
    captured: dict[str, Any] = {}

    def _fake_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(review_gh_module, "require_gh_min_version", lambda: None)
    monkeypatch.setattr(review_gh_module.subprocess, "run", _fake_run)

    assert _gh(["pr", "view", "1"]) == "ok"
    assert "env" not in captured


def test_ci_wait_imports_the_same_gated_gh_review_gh_exports() -> None:
    """[if] scripts/ci_wait.py's `_gh` is inspected [then] it IS
    scripts.review_gh's `_gh` object (an import, not a duplicate copy) -- the
    fact that makes gating `_gh` once in review_gh.py cover ci_wait.py too."""
    import scripts.ci_wait as ci_wait_module

    assert ci_wait_module._gh is _gh
