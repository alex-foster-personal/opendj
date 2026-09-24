"""The e2e failure artifact carries every suite's engine logs, not just Root's.

Twice on Sat 5 Sep 2026 (agentbox-2 18:16 UTC, agentbox-7 18:27 UTC) the Root
Playwright suite failed with "analysis stopped after a failure that is not
about these files (apps.analysis.run exited 5)". The traceback lives in the
engine's data dir under the suite's fixture root, which the failure artifact
did not include, and the next job's checkout cleaned it before anyone could
read it. A failure the artifact cannot explain is a failure that gets rerun.

The fix then named ONE dir, `root-playwright-data`, because that was the suite
in front of us. Every other suite in the same job seeds its own dir, so their
logs kept being discarded. Measured on issue #3477 (webkit loses its browser
mid-suite) on Tue 22 Sep 2026: two failures, one on `main` (83db39b6e8) and one
on PR #3548 (a59b88a37f), both `deckload-smoke.spec.ts`, both on runner
agentbox-2. Both artifacts were retained and both carry Root's logs, while
`deckload-data`'s -- the server behind the page that died -- were gone.

Regression lines:
  - if the failure artifact drops a suite's fixture data logs then the next
    crash in that suite is undiagnosable again
  - if the declared glob stops matching deckload-data then #3477's evidence
    is discarded again
"""

from __future__ import annotations

import glob as globlib
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
FIXTURE_LOGS = "apps/webui/frontend/tests/e2e/fixtures/*-data/logs/**"
#: The pattern this file used to pin. Kept only as the mutation control below.
SUPERSEDED = "apps/webui/frontend/tests/e2e/fixtures/root-playwright-data/logs/**"
E2E_DIR = REPO_ROOT / "apps" / "webui" / "frontend" / "tests" / "e2e"
#: `<name>-data`, as the configs and support helpers quote it -- either as a
#: bare path segment (`'deckload-data'` in a join) or inside a literal path.
_FIXTURE_DIR_RE = re.compile(r"""['"/]([a-z0-9]+(?:-[a-z0-9]+)*-data)['"/]""")


def _declared_paths() -> list[str]:
    doc = yaml.safe_load(E2E.read_text(encoding="utf-8"))
    uploads = [
        step
        for step in doc["jobs"]["gate"]["steps"]
        if "upload-artifact" in (step.get("uses") or "")
        and (step.get("with") or {}).get("name") == "e2e-gate-failures"
    ]
    assert len(uploads) == 1, "exactly one e2e-gate-failures upload"
    return uploads[0]["with"]["path"].split()


def _any_declared_match(relative_log: str, tmp: Path) -> bool:
    """Does ANY path the workflow declares select `relative_log`?

    Read from the workflow rather than from this file's constant, so narrowing
    the upload breaks these checks. A test named for "the declared glob" that
    actually asserts over a local constant passes on a workflow that no longer
    contains it.
    """
    return any(_matches(pattern, relative_log, tmp) for pattern in _declared_paths())


def _matches(pattern: str, relative_log: str, tmp: Path) -> bool:
    """Does `pattern` select `relative_log`, under real glob semantics?

    Evaluated against a real tree rather than a string comparison, because the
    whole question is whether `*` reaches across a directory name, and fnmatch
    answers that differently from the globbing upload-artifact performs.
    """
    target = tmp / relative_log
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    hits = globlib.glob(str(tmp / pattern), recursive=True)
    return str(target) in hits


def test_e2e_gate_failure_artifact_includes_every_fixture_log(tmp_path: Path) -> None:
    """if the artifact omits a suite's engine logs then that suite's crash has no trace"""
    paths = _declared_paths()
    assert FIXTURE_LOGS in paths, f"{FIXTURE_LOGS} missing from {paths}"


def test_the_declared_glob_reaches_the_suite_that_keeps_dying(tmp_path: Path) -> None:
    """if the glob misses deckload-data then issue #3477 loses its server side again"""
    assert _any_declared_match(
        "apps/webui/frontend/tests/e2e/fixtures/deckload-data/logs/webui-client-errors.log",
        tmp_path,
    ), "no declared artifact path reaches deckload-data"


def test_the_declared_glob_still_reaches_root(tmp_path: Path) -> None:
    """if widening the glob dropped Root then the Sat 5 Sep 2026 case regressed"""
    assert _any_declared_match(
        "apps/webui/frontend/tests/e2e/fixtures/root-playwright-data/logs/engine.log",
        tmp_path,
    ), "no declared artifact path reaches root-playwright-data"


def test_the_superseded_pattern_could_not_have_reached_deckload(tmp_path: Path) -> None:
    """if the old single-dir pattern already matched deckload-data then this change is a no-op

    The mutation control. Without it the three assertions above are satisfied by
    a pattern that never changed, and the file would pass while the evidence it
    exists to retain was still being thrown away.
    """
    assert not _matches(
        SUPERSEDED,
        "apps/webui/frontend/tests/e2e/fixtures/deckload-data/logs/webui-client-errors.log",
        tmp_path,
    ), "the superseded pattern already matched deckload-data, so nothing was fixed"


def test_every_fixture_data_dir_the_suites_name_is_covered(tmp_path: Path) -> None:
    """if a suite seeds a data dir the glob cannot name then its logs are discarded

    Read from the e2e sources rather than from disk: these dirs are seeded by a
    fixture at run time and are gitignored, so a checkout has none of them and a
    disk scan would assert over an empty set -- a check that cannot fail.
    """
    named = set()
    for source in E2E_DIR.rglob("*"):
        if source.suffix not in {".ts", ".py"} or not source.is_file():
            continue
        named |= set(_FIXTURE_DIR_RE.findall(source.read_text(encoding="utf-8")))
    assert "deckload-data" in named, "deckload-data is no longer named by any e2e source"
    assert "root-playwright-data" in named, "root-playwright-data is no longer named"
    for name in sorted(named):
        assert _any_declared_match(
            f"apps/webui/frontend/tests/e2e/fixtures/{name}/logs/engine.log",
            tmp_path,
        ), f"{name} is seeded by the suites but no declared artifact path reaches it"
