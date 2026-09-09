"""The drift gate must run where the quality gate runs it: with no app deps.

scripts/quality_gate.py measures from a THROWAWAY environment holding the
pinned tools in ops/quality/requirements.txt and nothing else. Its own header
says why -- "a quality score must not depend on whether an optional extra
resolved" -- and CI runs it exactly that way:

    uv run --isolated --no-project --python 3.11 \\
        --with-requirements ops/quality/requirements.txt \\
        python -m scripts.quality_gate

The sync-drift evaluator is the first one that imports the APPLICATION, to
build a real state.db and read it back. That is what makes it worth having,
and it is also what dragged a third-party dependency across that boundary: a
module-level ``import yaml`` in apps/database/generate_agents_md.py, on an
import path the linter only needs for sqlite introspection. Every local venv
has PyYAML, so the crash appeared nowhere except in CI, where it killed the
whole gate -- a traceback naming PyYAML rather than drift, on every PR, with
no report and no metrics. The first person under deadline deletes the
Evaluator line.

The invariant, rather than the incident: the drift gate's import graph
reaches NO third-party package. A future import that reintroduces one fails
here, by name, with the environment explained, instead of arriving as a red
CI job about something else.

Regression lines:
  - if scripts.sync_drift_lint imports a third-party package then broken
  - if the third-party probe reports none for a module that has one then broken
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: Distribution packages the repo itself provides. Everything else that is
#: not stdlib is a third party the quality gate's environment will not have.
FIRST_PARTY: frozenset[str] = frozenset({"apps", "scripts", "tests"})

_PROBE = """
import json, sys
before = set(sys.modules)
__import__({module!r})
tops = {{name.split('.')[0] for name in set(sys.modules) - before}}
tops |= {{{module!r}.split('.')[0]}}
print(json.dumps(sorted(
    t for t in tops
    if t and not t.startswith('_')
    and t not in sys.stdlib_module_names
    and t not in {first_party!r}
)))
"""


def _third_party_imports(module: str) -> list[str]:
    """Top-level non-stdlib, non-first-party modules importing ``module`` pulls.

    Run in a FRESH interpreter: this process has already imported the whole
    test suite, so measuring ``sys.modules`` here would answer a different
    question entirely.
    """
    probe = _PROBE.format(module=module, first_party=sorted(FIRST_PARTY))
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    return list(json.loads(done.stdout))


def test_the_probe_reports_a_third_party_import_when_there_is_one() -> None:
    """The control, derived from the hypothesis rather than a clean corpus.

    The claim is that a named module's import graph is free of third parties.
    That claim predicts this probe CAN name one, so it is pointed where the
    hypothesis says one lives: apps.sync_hub.service is the SIBLING of the
    apps.sync_hub.protocol_common the linter does import, one module deeper
    into the same package, and it pulls fastapi and pydantic. A probe that
    reported an empty list for everything would pass the real test below for
    the wrong reason, and would keep passing after it broke.
    """
    found = _third_party_imports("apps.sync_hub.service")

    assert "fastapi" in found and "pydantic" in found


def test_the_drift_gate_imports_no_third_party_package() -> None:
    """What the quality gate's environment can actually satisfy."""
    found = _third_party_imports("scripts.sync_drift_lint")

    assert found == [], (
        f"scripts.sync_drift_lint now imports {found}, which the quality "
        "gate's environment does not have: ops/quality/requirements.txt "
        "holds the pinned measurement tools only, deliberately. CI runs "
        "`uv run --isolated --no-project --with-requirements "
        "ops/quality/requirements.txt python -m scripts.quality_gate`, and "
        "that run will die with ModuleNotFoundError before any check "
        "executes. Either make the new import local to the function that "
        "needs it (as apps/database/generate_agents_md.py does with PyYAML) "
        "or decide, deliberately, to move the drift gate out of the quality "
        "ratchet."
    )


def test_the_subject_builder_imports_no_third_party_package() -> None:
    """The half that reaches furthest into the app, checked separately.

    Named on its own so a failure says WHICH side grew the dependency: the
    subject builder imports nine application modules, the checks import four.
    """
    assert _third_party_imports("scripts.sync_drift_subject") == []
