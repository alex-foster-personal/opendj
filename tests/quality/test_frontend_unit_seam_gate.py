"""Frontend unit tests must not reference production seams that do not exist.

Issue #3658: #3643 merged a test calling glue._meterPumpArmedForTests() while
that export never existed on main. A red trunk hid the TypeError among real
logic failures. GUARD-8229 adds a static check before pnpm test:unit runs.

[if] a unit test names a missing *ForTests seam [then] the guard fails naming it, [else stop].

Regression lines:
  - if the live tree references a missing *ForTests export then the guard
    fails naming the symbol, so broken
  - if the 76dcf21ba midi-core test were still present then the guard would
    name _meterPumpArmedForTests, so broken
  - if a seam call's binding cannot be resolved (named import, fixture entry,
    parameter) then the guard reports it instead of dropping it, so broken
  - if two test callbacks reuse one local name for different modules then
    each call is checked against its own scope's module, so broken
  - if a seam is imported or destructured under another local name then the
    guard still checks it under the export name, so broken
  - if a seam's module arrives through a destructuring assignment, a const
    entry source or an imported helper function then the guard follows it
    instead of failing the live tree as unverifiable, so broken
  - if two pytest-xdist workers scan scratch files at once then each has its
    own scratch directory and neither cleanup trips over the other, so broken
  - if a test imports a seam a test-side helper re-exports from production
    (named, renamed, namespace or export *) then the guard checks it on the
    production module under its original name, and a re-export cycle is
    unverifiable, so broken
  - if a seam is invoked through .call, .apply or .bind then the guard checks
    the seam the call reads, so broken
  - if a seam is invoked through a computed member whose key is a const
    string then the guard checks the seam the key resolves to, so broken
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.perf.node_runtime import resolved_node

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ROOT = REPO_ROOT / "apps" / "webui" / "frontend"
SCANNER = FRONTEND_ROOT / "scripts" / "unit-test-seam-check.mjs"
COMMIT_3643 = "76dcf21ba"
MIDI_CORE_PATH = "apps/webui/frontend/tests/unit/midi-core.test.mjs"


# The scanner imports the `typescript` package from frontend node_modules,
# which ci.yml's fast pytest lanes (and full-ci.yml's coverage lane) never
# install. Those lanes `--ignore` this file, exactly like
# test_frontend_typing_gate.py, and ci.yml's `quality` job, which installs
# node_modules, runs it as a hard PR gate. No skipif: a run that lacks the
# dependency must fail loud, never read as a pass.
# test_frontend_unit_seam_wiring.py pins that the file is collected there.
pytestmark = pytest.mark.requirement("GUARD-8229")


def _run_scanner(
    *,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        [resolved_node(), str(SCANNER)],
        cwd=cwd or FRONTEND_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@contextmanager
def _scratch_dir() -> Iterator[Path]:
    """A scratch directory of this test's own under tests/unit.

    One per call, never shared: under ``-n auto`` two workers scanning at once
    must not remove each other's files or trip on each other's cleanup. It is
    a dot-directory directly under tests/unit, so relative imports such as
    ``../load-typescript.mjs`` resolve as they do for a real unit test, and the
    scanner's own walk (which skips dot-directories) never scans another
    worker's scratch file as part of the live tree.
    """
    scratch = Path(tempfile.mkdtemp(prefix=".tmp-seam-guard-", dir=FRONTEND_ROOT / "tests" / "unit"))
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _scan_files(paths: list[Path]) -> subprocess.CompletedProcess[str]:
    rels = ",".join(str(path.relative_to(FRONTEND_ROOT)) for path in paths)
    return _run_scanner(extra_env={"UNIT_TEST_SEAM_EXTRA": rels})


def _scan_scratch(
    name: str,
    lines: list[str],
    helpers: dict[str, list[str]] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Scan the live tree plus one scratch test file written from ``lines``.

    ``helpers`` are test-side modules written beside it (importable as
    ``./<name>``) but not scanned as test files themselves.
    """
    with _scratch_dir() as scratch:
        for helper_name, helper_lines in (helpers or {}).items():
            (scratch / helper_name).write_text("\n".join(helper_lines) + "\n", encoding="utf-8")
        path = scratch / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return _scan_files([path])


_LOADER_IMPORTS = [
    "import { test } from 'node:test';",
    "import { loadTypeScriptModule } from '../load-typescript.mjs';",
]
_GLUE = "src/lib/rb/midi/action-glue.svelte.ts"
_WEBMIDI = "src/lib/rb/midi/webmidi.svelte.ts"


def test_live_tree_passes() -> None:
    """[if] current branch after orphan test removal [then] scanner exits 0, [else stop]."""
    result = _run_scanner()
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["filesScanned"] > 0
    # Presence, not absence: the live tree must actually verify seams, so a
    # resolver that silently classified everything as test-local fails here.
    assert summary["seamsChecked"] > 0, summary


def test_names_missing_export_like_3643() -> None:
    """[if] a test calls a missing *ForTests seam [then] stderr names it, [else stop]."""
    result = _scan_scratch(
        "orphan-seam.test.mjs",
        [
            "import { before, test } from 'node:test';",
            "import { loadTypeScriptModule } from '../load-typescript.mjs';",
            "let glue;",
            "before(async () => {",
            "  glue = await loadTypeScriptModule('src/lib/rb/midi/action-glue.svelte.ts');",
            "});",
            "test('orphan seam', () => { glue._meterPumpArmedForTests(); });",
        ],
    )
    assert result.returncode == 1
    assert "missing export _meterPumpArmedForTests" in result.stderr
    assert "action-glue.svelte.ts" in result.stderr


def test_ignores_fixture_stubs() -> None:
    """[if] *ForTests appears only under tests/unit/fixtures [then] scanner passes, [else stop]."""
    stub = FRONTEND_ROOT / "tests" / "unit" / "fixtures" / "stretch-deck-processor-load-stub.ts"
    assert stub.is_file()
    assert "ForTests" in stub.read_text(encoding="utf-8")
    result = _run_scanner()
    assert result.returncode == 0, result.stderr


def test_comment_mentions_do_not_count() -> None:
    """[if] *ForTests appears only in a comment [then] scanner passes, [else stop]."""
    result = _scan_scratch(
        "comment-only.test.mjs",
        ["// glue._fooForTests()", "import { test } from 'node:test';", "test('ok', () => {});"],
    )
    assert result.returncode == 0, result.stderr


def _git() -> str:
    """The git executable, resolved once; a host without it fails here, by name."""
    git = shutil.which("git")
    if git is None:
        raise AssertionError("git is not on PATH: test_would_have_caught_76dcf21ba needs it to read 76dcf21ba")
    return git


def test_would_have_caught_76dcf21ba() -> None:
    """[if] midi-core.test.mjs at 76dcf21ba [then] guard names missing seam, [else stop]."""
    git = _git()
    subprocess.run(
        [git, "cat-file", "-e", f"{COMMIT_3643}^{{commit}}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    show = subprocess.run(
        [git, "show", f"{COMMIT_3643}:{MIDI_CORE_PATH}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    result = _scan_scratch("midi-core-3643.test.mjs", show.stdout.splitlines())
    assert result.returncode == 1
    assert "missing export _meterPumpArmedForTests" in result.stderr


def test_named_import_of_missing_seam_is_flagged() -> None:
    """[if] a named import names a missing seam [then] the guard names it, [else stop]."""
    result = _scan_scratch(
        "named-import.test.mjs",
        [
            "import { test } from 'node:test';",
            "import { _missingNamedImportForTests } from '$lib/rb/midi/action-glue.svelte';",
            "test('named import', () => { _missingNamedImportForTests(); });",
        ],
    )
    assert result.returncode == 1, result.stdout
    assert "missing export _missingNamedImportForTests on " + _GLUE in result.stderr


def test_fixture_entry_missing_seam_is_flagged() -> None:
    """[if] a fixture entry lacks the seam [then] the guard names it, [else stop]."""
    entry = "tests/unit/fixtures/first-run-boot-gate-entry.ts"
    result = _scan_scratch(
        "fixture-entry.test.mjs",
        [
            *_LOADER_IMPORTS,
            f"const mod = await loadTypeScriptModule('{entry}');",
            "test('fixture', () => { mod._missingFixtureSeamForTests(); });",
        ],
    )
    assert result.returncode == 1, result.stdout
    assert f"missing export _missingFixtureSeamForTests on {entry}" in result.stderr


def test_unfollowable_seam_call_is_flagged() -> None:
    """[if] a seam binding cannot be followed [then] the guard fails naming it, [else stop]."""
    result = _scan_scratch(
        "parameter.test.mjs",
        [
            "import { test } from 'node:test';",
            "function reset(m) { m._resetForTests(); }",
            "test('param', () => { reset({}); });",
        ],
    )
    assert result.returncode == 1, result.stdout
    assert "unverifiable seam call m._resetForTests(): function parameter" in result.stderr


def test_test_local_seam_helpers_pass() -> None:
    """[if] a *ForTests callable is built in the test file [then] the guard passes, [else stop]."""
    result = _scan_scratch(
        "local-helper.test.mjs",
        [
            "import { test } from 'node:test';",
            "function _localForTests() {}",
            "const helper = { _tickForTests() {} };",
            "test('local', () => { _localForTests(); helper._tickForTests(); });",
        ],
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["localSeamCalls"] >= 2, summary


def test_scoped_name_reuse_flags_the_call_in_its_own_scope() -> None:
    """[if] a scope's own module lacks the seam [then] the guard names that module, [else stop]."""
    result = _scan_scratch(
        "scope-missing.test.mjs",
        [
            *_LOADER_IMPORTS,
            "test('a', async () => {",
            f"  const mod = await loadTypeScriptModule('{_WEBMIDI}');",
            "  mod._resetMasterCueForTests();",
            "});",
            "test('b', async () => {",
            f"  const mod = await loadTypeScriptModule('{_GLUE}');",
            "  mod._resetMasterCueForTests();",
            "});",
        ],
    )
    assert result.returncode == 1, result.stdout
    assert f"missing export _resetMasterCueForTests on {_WEBMIDI}" in result.stderr
    assert f"on {_GLUE}" not in result.stderr


def test_scoped_name_reuse_does_not_blame_another_scope() -> None:
    """[if] each scope's module has its seam [then] the guard passes, [else stop]."""
    result = _scan_scratch(
        "scope-valid.test.mjs",
        [
            *_LOADER_IMPORTS,
            "test('a', async () => {",
            f"  const mod = await loadTypeScriptModule('{_GLUE}');",
            "  mod._resetMasterCueForTests();",
            "});",
            "test('b', async () => {",
            f"  const mod = await loadTypeScriptModule('{_WEBMIDI}');",
            "  mod._resetMidiForTests();",
            "});",
        ],
    )
    assert result.returncode == 0, result.stderr


def test_reassigned_binding_is_checked_against_every_candidate() -> None:
    """[if] one binding can hold two modules [then] the seam must exist on both, [else stop]."""
    result = _scan_scratch(
        "reassigned.test.mjs",
        [
            *_LOADER_IMPORTS,
            "let mod;",
            f"test('a', async () => {{ mod = await loadTypeScriptModule('{_GLUE}'); }});",
            f"test('b', async () => {{ mod = await loadTypeScriptModule('{_WEBMIDI}'); }});",
            "test('c', () => { mod._resetMidiForTests(); });",
        ],
    )
    assert result.returncode == 1, result.stdout
    assert f"missing export _resetMidiForTests on {_GLUE}" in result.stderr
    assert "can hold 2 different modules" in result.stderr
    assert f"on {_WEBMIDI}" not in result.stderr


def _live_summary() -> dict[str, int]:
    result = _run_scanner()
    assert result.returncode == 0, result.stderr
    summary: dict[str, int] = json.loads(result.stdout)
    return summary


def test_aliased_named_import_of_missing_seam_is_flagged() -> None:
    """[if] a missing seam is imported under another name [then] the guard names it, [else stop]."""
    result = _scan_scratch(
        "aliased-import.test.mjs",
        [
            "import { test } from 'node:test';",
            "import { _missingAliasedImportForTests as reset } from '$lib/rb/midi/action-glue.svelte';",
            "test('aliased import', () => { reset(); });",
        ],
    )
    assert result.returncode == 1, result.stdout
    assert "missing export _missingAliasedImportForTests on " + _GLUE in result.stderr


def test_aliased_destructuring_of_missing_seam_is_flagged() -> None:
    """[if] a missing seam is destructured under another name [then] the guard names it, [else stop]."""
    result = _scan_scratch(
        "aliased-destructure.test.mjs",
        [
            *_LOADER_IMPORTS,
            f"const mod = await loadTypeScriptModule('{_GLUE}');",
            "const { _missingAliasedBindingForTests: reset } = mod;",
            "const alsoReset = mod._missingAliasedMemberForTests;",
            "test('aliased binding', () => { reset(); alsoReset(); });",
        ],
    )
    assert result.returncode == 1, result.stdout
    assert "missing export _missingAliasedBindingForTests on " + _GLUE in result.stderr
    assert "missing export _missingAliasedMemberForTests on " + _GLUE in result.stderr


def test_aliased_existing_seams_are_checked_and_pass() -> None:
    """[if] real seams are aliased [then] exactly those calls are verified and pass, [else stop].

    Control for the alias fix in both directions: the aliased seams must be
    COUNTED (a guard that still ignored aliases would add 0), and an aliased
    production call that is not a seam must NOT be counted (a guard that
    treated every resolved call as a seam would add 3).
    """
    live = _live_summary()
    result = _scan_scratch(
        "aliased-valid.test.mjs",
        [
            *_LOADER_IMPORTS,
            "import { _resetMasterCueForTests as resetCue, attachMidiGlue as attach } "
            "from '$lib/rb/midi/action-glue.svelte';",
            f"const {{ _resetMidiForTests: resetMidi }} = await loadTypeScriptModule('{_WEBMIDI}');",
            "test('aliased valid', () => { resetCue(); resetMidi(); attach(); });",
        ],
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["seamsChecked"] == live["seamsChecked"] + 2, (summary, live)


def test_destructuring_assignment_is_followed() -> None:
    """[if] a seam's module is bound by destructuring assignment [then] it is checked, [else stop].

    The shape tests/unit/iopin-12-stall-rebuild.test.mjs uses; the earlier
    guard read the shorthand target's PROPERTY symbol and reported the call as
    never assigned, which is what failed the Trunk queue run for this PR.
    """
    entry = "tests/unit/fixtures/djio-fallback-entry.ts"
    result = _scan_scratch(
        "destructure-assign.test.mjs",
        [
            *_LOADER_IMPORTS,
            "let audio;",
            "let renamed;",
            f"const entry = await loadTypeScriptModule('{entry}');",
            "({ audio, audio: renamed } = entry);",
            "test('assign', () => { audio._missingAssignedForTests(); renamed._missingRenamedForTests(); });",
        ],
    )
    assert result.returncode == 1, result.stdout
    assert f"missing export _missingAssignedForTests on {entry} as audio._missingAssignedForTests" in result.stderr
    assert f"missing export _missingRenamedForTests on {entry} as audio._missingRenamedForTests" in result.stderr
    assert "unverifiable" not in result.stderr, result.stderr


def test_const_entry_source_is_followed() -> None:
    """[if] loadSvelteSsrModule gets a const joined entry [then] its re-exports are checked, [else stop]."""
    result = _scan_scratch(
        "const-entry.test.mjs",
        [
            "import { test } from 'node:test';",
            "import { loadSvelteSsrModule } from '../load-svelte-ssr.mjs';",
            "const ENTRY = [",
            "  \"export { setupWizard } from '$lib/setup/wizard.svelte';\",",
            "  \"export { render } from 'svelte/server';\"",
            "].join('\\n');",
            "const mod = await loadSvelteSsrModule(ENTRY);",
            "test('entry', () => { mod.setupWizard._missingEntrySeamForTests(); });",
        ],
    )
    assert result.returncode == 1, result.stdout
    assert "missing export _missingEntrySeamForTests on src/lib/setup/wizard.svelte.ts" in result.stderr
    assert "unverifiable" not in result.stderr, result.stderr


def test_imported_helper_function_is_followed() -> None:
    """[if] a test-side helper returns the module [then] the seam is checked on it, [else stop]."""
    result = _scan_scratch(
        "helper-function.test.mjs",
        [
            "import { test } from 'node:test';",
            "import { loadRegistry } from '../../../scripts/export-midi-maps.mjs';",
            "test('helper', async () => {",
            "  const { webmidi } = await loadRegistry();",
            "  webmidi._missingHelperSeamForTests();",
            "});",
        ],
    )
    assert result.returncode == 1, result.stdout
    assert f"missing export _missingHelperSeamForTests on {_WEBMIDI}" in result.stderr
    assert "unverifiable" not in result.stderr, result.stderr


def test_concurrent_scratch_scans_do_not_collide() -> None:
    """[if] another worker's scratch file is live [then] this scan and cleanup still pass, [else stop].

    Holds one scratch directory open, as a second pytest-xdist worker would,
    while a scan runs and cleans up its own. A shared directory fails this
    with ``Directory not empty`` on the inner cleanup. The file count is the
    control the other way: the held file sits in a dot-directory, so it must
    not be scanned as part of the live tree.
    """
    live = _live_summary()
    with _scratch_dir() as held:
        held_file = held / "held.test.mjs"
        held_file.write_text("import { test } from 'node:test';\n", encoding="utf-8")
        result = _scan_scratch(
            "inner.test.mjs",
            ["import { test } from 'node:test';", "test('ok', () => {});"],
        )
        assert held_file.is_file(), "the inner scan's cleanup removed another worker's file"
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["filesScanned"] == live["filesScanned"] + 1
    assert not held.exists()


_GLUE_SPEC = "$lib/rb/midi/action-glue.svelte"
_REEXPORT_HELPERS = {
    "named.mjs": [
        f"export {{ _missingReExportedForTests, _resetMasterCueForTests }} from '{_GLUE_SPEC}';",
        f"export {{ _missingRenamedForTests as resetAlias, _resetMasterCueForTests as resetCue }} from '{_GLUE_SPEC}';",
    ],
    "namespace.mjs": [f"export * as glue from '{_GLUE_SPEC}';"],
    "star.mjs": [f"export * from '{_GLUE_SPEC}';"],
    "two-hop.mjs": [
        "export { _missingTwoHopForTests } from './star.mjs';",
        "export { resetCue as hopCue } from './named.mjs';",
    ],
    "local.mjs": [
        f"import {{ _missingLocalReExportForTests }} from '{_GLUE_SPEC}';",
        "export { _missingLocalReExportForTests };",
    ],
}


def test_helper_re_exported_missing_seams_are_flagged() -> None:
    """[if] a helper re-exports a missing production seam [then] the guard names it there, [else stop].

    Devin #4197017240: an import from a test-side helper was classified as
    test-local, so a production seam deleted behind a re-export passed.
    """
    result = _scan_scratch(
        "reexport-missing.test.mjs",
        [
            "import { test } from 'node:test';",
            "import { _missingReExportedForTests, resetAlias } from './named.mjs';",
            "import { glue } from './namespace.mjs';",
            "import * as star from './star.mjs';",
            "import { _missingTwoHopForTests } from './two-hop.mjs';",
            "import { _missingLocalReExportForTests } from './local.mjs';",
            "test('re-exports', async () => {",
            "  _missingReExportedForTests();",
            "  resetAlias();",
            "  glue._missingNamespaceForTests();",
            "  star._missingStarForTests();",
            "  _missingTwoHopForTests();",
            "  _missingLocalReExportForTests();",
            "  const dyn = await import('./named.mjs');",
            "  dyn._missingReExportedForTests();",
            "});",
        ],
        helpers=_REEXPORT_HELPERS,
    )
    assert result.returncode == 1, result.stdout
    for seam in (
        "_missingReExportedForTests",
        "_missingRenamedForTests",
        "_missingNamespaceForTests",
        "_missingStarForTests",
        "_missingTwoHopForTests",
        "_missingLocalReExportForTests",
    ):
        assert f"missing export {seam} on {_GLUE} as {seam} " in result.stderr, (seam, result.stderr)
    # The dynamic import of the helper is checked too, not only the static one.
    assert result.stderr.count("missing export _missingReExportedForTests on") == 2, result.stderr
    assert "unverifiable" not in result.stderr, result.stderr


def test_helper_re_exported_existing_seams_pass_and_are_counted() -> None:
    """[if] a helper re-exports real seams [then] each call is verified and passes, [else stop].

    Control the other way: a guard that flagged every helper import, or kept
    calling them test-local, fails the count rather than the exit code.
    """
    live = _live_summary()
    result = _scan_scratch(
        "reexport-valid.test.mjs",
        [
            "import { test } from 'node:test';",
            "import { _resetMasterCueForTests, resetCue } from './named.mjs';",
            "import { glue } from './namespace.mjs';",
            "import * as star from './star.mjs';",
            "import { hopCue } from './two-hop.mjs';",
            "test('re-exports', () => {",
            "  _resetMasterCueForTests();",
            "  resetCue();",
            "  glue._resetMasterCueForTests();",
            "  star._resetMasterCueForTests();",
            "  hopCue();",
            "});",
        ],
        helpers=_REEXPORT_HELPERS,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["seamsChecked"] == live["seamsChecked"] + 5, (summary, live)
    assert summary["localSeamCalls"] == live["localSeamCalls"], (summary, live)


def test_star_barrel_follows_the_one_star_that_exports_the_seam() -> None:
    """[if] two export * sources and only one exports the seam [then] it is checked there, [else stop].

    Codex #4237404166: every star source was a candidate whether or not it
    exported the name, so a valid seam behind a two-star barrel read as
    ambiguous. ESM resolves it to its one supplier; so does the guard.
    """
    live = _live_summary()
    helpers = {"two-star.mjs": [f"export * from '{_GLUE_SPEC}';", "export * from '$lib/rb/midi/webmidi.svelte';"]}
    valid = _scan_scratch(
        "two-star-valid.test.mjs",
        [
            "import { test } from 'node:test';",
            "import { _resetMasterCueForTests } from './two-star.mjs';",
            "test('two stars', () => { _resetMasterCueForTests(); });",
        ],
        helpers=helpers,
    )
    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout)["seamsChecked"] == live["seamsChecked"] + 1
    missing = _scan_scratch(
        "two-star-missing.test.mjs",
        [
            "import { test } from 'node:test';",
            "import { _missingTwoStarForTests } from './two-star.mjs';",
            "test('two stars', () => { _missingTwoStarForTests(); });",
        ],
        helpers=helpers,
    )
    assert missing.returncode == 1, missing.stdout
    assert f"missing export _missingTwoStarForTests on {_GLUE} " in missing.stderr, missing.stderr
    assert f"missing export _missingTwoStarForTests on {_WEBMIDI} " in missing.stderr, missing.stderr
    assert "may come from" not in missing.stderr, missing.stderr


def test_re_export_cycle_is_unverifiable() -> None:
    """[if] helper re-exports form a cycle or name nothing [then] the call is unverifiable, [else stop]."""
    result = _scan_scratch(
        "reexport-cycle.test.mjs",
        [
            "import { test } from 'node:test';",
            "import { _cycleForTests } from './cycle-a.mjs';",
            "import { _absentForTests } from './cycle-b.mjs';",
            "test('cycle', () => { _cycleForTests(); _absentForTests(); });",
        ],
        helpers={
            "cycle-a.mjs": ["export { _cycleForTests } from './cycle-b.mjs';"],
            "cycle-b.mjs": ["export { _cycleForTests } from './cycle-a.mjs';"],
        },
    )
    assert result.returncode == 1, result.stdout
    assert "unverifiable seam call _cycleForTests(): re-export cycle through" in result.stderr, result.stderr
    assert "unverifiable seam call _absentForTests(): test-side module" in result.stderr, result.stderr
    assert "does not export `_absentForTests`" in result.stderr, result.stderr


def test_seam_invoked_through_call_apply_bind_is_checked() -> None:
    """[if] a missing seam is invoked via .call/.apply/.bind [then] the guard names it, [else stop].

    Devin #4197017656: the collector read the final member (`call`) as the
    callee name, so `mod._missingForTests.call(mod)` was never a seam call.
    """
    result = _scan_scratch(
        "invoke-members.test.mjs",
        [
            *_LOADER_IMPORTS,
            f"const mod = await loadTypeScriptModule('{_GLUE}');",
            "test('invoke', () => {",
            "  mod._missingCallForTests.call(mod);",
            "  mod._missingApplyForTests.apply(mod, []);",
            "  mod._missingBindForTests.bind(mod)();",
            "  mod['_missingElementCallForTests']['call'](mod);",
            "});",
        ],
    )
    assert result.returncode == 1, result.stdout
    for seam in (
        "_missingCallForTests",
        "_missingApplyForTests",
        "_missingBindForTests",
        "_missingElementCallForTests",
    ):
        assert f"missing export {seam} on {_GLUE} as {seam} " in result.stderr, (seam, result.stderr)


def test_existing_seam_invoked_through_call_passes_and_is_counted() -> None:
    """[if] a real seam is invoked via .call/.apply/.bind [then] it is verified and passes, [else stop]."""
    live = _live_summary()
    result = _scan_scratch(
        "invoke-valid.test.mjs",
        [
            *_LOADER_IMPORTS,
            f"const mod = await loadTypeScriptModule('{_GLUE}');",
            "const reset = mod._resetMasterCueForTests;",
            "test('invoke', () => {",
            "  mod._resetMasterCueForTests.call(mod);",
            "  reset.apply(null, []);",
            "  const notASeam = { call() {} };",
            "  notASeam.call();",
            "});",
        ],
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["seamsChecked"] == live["seamsChecked"] + 2, (summary, live)


def test_computed_const_key_missing_seam_is_reported() -> None:
    """[if] a missing seam is invoked as mod[key]() with a const *ForTests key [then] the guard names it, [else stop]."""
    result = _scan_scratch(
        "computed-missing.test.mjs",
        [
            *_LOADER_IMPORTS,
            f"const mod = await loadTypeScriptModule('{_GLUE}');",
            "const key = '_missingForTests';",
            "test('computed', () => { mod[key](); });",
        ],
    )
    assert result.returncode == 1, result.stdout
    assert f"missing export _missingForTests on {_GLUE} as _missingForTests " in result.stderr, result.stderr


def test_computed_const_key_existing_seam_passes_and_direct_call_still_collected() -> None:
    """[if] a real seam is invoked as mod[key]() and as mod._xForTests() [then] both are verified and counted, [else stop]."""
    live = _live_summary()
    result = _scan_scratch(
        "computed-valid.test.mjs",
        [
            *_LOADER_IMPORTS,
            f"const mod = await loadTypeScriptModule('{_GLUE}');",
            "const key = '_resetMasterCueForTests';",
            "test('computed', () => {",
            "  mod[key]();",
            "  mod._resetMasterCueForTests();",
            "});",
        ],
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["seamsChecked"] == live["seamsChecked"] + 2, (summary, live)
