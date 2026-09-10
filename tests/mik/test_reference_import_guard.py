"""NATIVE-12: nothing imports the MIK reference corpus without a decision.

``data/reference/mik/<date>/`` holds a captured Mixed In Key run. NATIVE-12 says
it is reference data only: anything taken from it into files, the odj library or
``state.db`` needs its OWN recorded decision first. Nothing is imported today,
and this suite is what keeps that true by intent rather than by accident.

Absence is the easy half to fake. A test that greps the tree for an importer and
finds none passes just as loudly when it is broken, when it is pointed at the
wrong tree, and when the importer it was written to stop has not been written
yet. So this suite proves the PRESENCE of the guard in three parts
(``.claude/rules/verification.md``):

  * the refusal itself, ``apps.mik.reference_guard.require_mik_import_decision``,
    admits a registered module and refuses an unregistered one;
  * the static sweep is fed synthetic modules that DO import the corpus, one per
    shape an importer can take -- literal path, joined parts, ``os.path.join``,
    a Windows separator, and a call through the guard's own path accessor -- and
    must report every one of them, while a docstring mention and a sibling path
    must stay silent;
  * the sweep is then run at a purpose-built tree carrying an unregistered
    importer, and must fail there. That control is the difference between "no
    importer exists" and "an importer cannot exist unnoticed".

A module that names the corpus is either an importer (needs a decision in
``apps/mik/import_decisions.json``, backed by a record document) or a declared
definition site (DEFINITION_SITES below, each with a reason). Adding to either
list is the deliberate act NATIVE-12 exists to force.

The whole contract in one line:
  - [if] a module names the corpus undecided [then] the sweep fails it, [else stop].

Regression lines:
  - if a module names the corpus without a recorded import decision then broken
  - if the sweep cannot report a module that imports the corpus then broken
  - if a docstring mention or a sibling path is reported then broken
  - if the guard admits a module that is not in the registry then broken
  - if the guard refuses a module that IS in the registry then broken
  - if a recorded decision names a record document that does not exist then broken
  - if a decision outlives the importer it decided then broken
  - if an import decision is recorded without editing this suite then broken
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from apps.mik import reference_guard

pytestmark = pytest.mark.requirement("NATIVE-12")

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
SCAN_ROOTS: tuple[str, ...] = ("apps", "scripts", "tests", "tools")
SKIPPED_DIR_NAMES: frozenset[str] = frozenset({"__pycache__", "_vendor", "payload", "target"})

# Modules allowed to NAME the corpus path in code without an import decision,
# with why. Neither reads a byte of it: the guard declares where the corpus is,
# and this suite's corpus paths are strings it hands to the scanner.
DEFINITION_SITES: dict[str, str] = {
    "apps/mik/reference_guard.py": (
        "declares the corpus location and the refusal; it opens no corpus file, and a "
        "caller still cannot obtain the root without a recorded decision"
    ),
    "tests/mik/test_reference_import_guard.py": (
        "the sweep and its synthetic control sources; every corpus path here is text "
        "fed to the scanner, not a location this module reads"
    ),
}

# Names whose use in code means "I am holding the corpus path", so an importer
# that takes its path from the guard instead of writing the literal is still
# classified. Without this the sweep would only see modules that spell it out.
CORPUS_HANDLE_NAMES: frozenset[str] = frozenset(
    {"corpus_root", "corpus_segments", "MIK_REFERENCE_ROOT"}
)

PATH_FACTORY_NAMES: frozenset[str] = frozenset(
    {"Path", "PurePath", "PosixPath", "PurePosixPath", "WindowsPath", "PureWindowsPath", "join"}
)


# ------------------------------------------------------------- source scanning


def _corpus_path() -> str:
    """The corpus location as the registry states it, slash-joined."""
    return "/".join(reference_guard.corpus_segments())


def _corpus_tail() -> tuple[str, str]:
    """The last two segments of the corpus path, from the registry that owns it."""
    first, second = reference_guard.corpus_segments()[-2:]
    return first, second


def _split_segments(text: str) -> list[str]:
    """Split a path-ish string into its segments, either separator, case-folded."""
    return [part.strip().lower() for part in text.replace("\\", "/").split("/")]


def _names_corpus(segments: list[str]) -> bool:
    """Does this ordered segment list contain the corpus tail, adjacent?"""
    tail = _corpus_tail()
    width = len(tail)
    return any(tuple(segments[i : i + width]) == tail for i in range(len(segments) - width + 1))


def _docstring_constants(tree: ast.Module) -> set[int]:
    """``id()`` of every string constant that is a docstring, not a value.

    A path mentioned in prose is documentation. Counting it would make the sweep
    cry wolf on every comment that explains the rule it enforces.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _literal_parts(node: ast.AST) -> list[str] | None:
    """Ordered literal segments of a path expression built from strings, or None.

    Handles ``Path("a", "b")``, ``os.path.join("a", "b")`` and the ``/`` operator
    chained over literals and over a variable base. A non-literal operand
    contributes nothing rather than making the whole expression opaque: the
    corpus tail only has to appear somewhere in the literal run for the read to
    be classified, and ``DATA_DIR / "reference" / "mik"`` is exactly that shape.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _split_segments(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return (_literal_parts(node.left) or []) + (_literal_parts(node.right) or [])
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in PATH_FACTORY_NAMES:
            return None
        parts: list[str] = []
        for arg in node.args:
            parts += _literal_parts(arg) or []
        return parts
    return None


def _corpus_sites(tree: ast.Module) -> list[str]:
    """One line-numbered description per line that names the corpus."""
    docstrings = _docstring_constants(tree)
    sites: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in CORPUS_HANDLE_NAMES:
            if isinstance(node.ctx, ast.Load):
                sites.setdefault(node.lineno, f"line {node.lineno}: name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in CORPUS_HANDLE_NAMES:
            sites.setdefault(node.lineno, f"line {node.lineno}: attribute {node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if _names_corpus(_split_segments(node.value)):
                sites.setdefault(node.lineno, f"line {node.lineno}: {node.value!r}")
        elif isinstance(node, ast.BinOp | ast.Call):
            parts = _literal_parts(node)
            if parts is not None and _names_corpus(parts):
                sites.setdefault(node.lineno, f"line {node.lineno}: {'/'.join(parts)}")
    return [sites[line] for line in sorted(sites)]


def _scanned_files(root: Path) -> list[Path]:
    """Every Python file under the scan roots, sorted, caches and vendored trees skipped."""
    files: list[Path] = []
    for scan_root in SCAN_ROOTS:
        base = root / scan_root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if SKIPPED_DIR_NAMES & set(path.parts):
                continue
            files.append(path)
    return files


def _corpus_namers(root: Path) -> dict[str, list[str]]:
    """Relative path -> reported sites, for every module under ``root`` naming the corpus."""
    found: dict[str, list[str]] = {}
    for path in _scanned_files(root):
        source = path.read_text(encoding="utf-8")
        sites = _corpus_sites(ast.parse(source, filename=str(path)))
        if sites:
            found[path.relative_to(root).as_posix()] = sites
    return found


def _unregistered_namers(root: Path, registry_path: Path) -> dict[str, list[str]]:
    """Namers under ``root`` that are neither a decision holder nor a definition site.

    This is the sweep CI runs. ``root`` is a parameter so the controls below can
    aim it at a tree whose importer is known to be there.
    """
    registered = {
        decision.module for decision in reference_guard.load_import_decisions(registry_path)
    }
    return {
        module: sites
        for module, sites in _corpus_namers(root).items()
        if module not in registered and module not in DEFINITION_SITES
    }


def _stale_decisions(root: Path, registry_path: Path) -> list[str]:
    """Registered modules that no longer reach for the corpus.

    A decision that outlives its importer stops being the record of one import
    and becomes an exemption carried forward, so it rots the way a stale
    definition site does.
    """
    namers = set(_corpus_namers(root))
    return sorted(
        decision.module
        for decision in reference_guard.load_import_decisions(registry_path)
        if decision.module not in namers
    )


# ------------------------------------------------------------ control fixtures


def _control_tree(root: Path, sources: dict[str, str]) -> Path:
    """Write ``sources`` as a tree of modules under ``root`` and return ``root``."""
    for relative, text in sources.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _imports_the_corpus() -> str:
    """A module body that reaches for the corpus through joined literal parts."""
    reference, mik = _corpus_tail()
    return f'ROOT = DATA_DIR / "{reference}" / "{mik}"\n'


def _write_registry(path: Path, decisions: list[dict[str, str]]) -> Path:
    """Write a throwaway registry, taking corpus_path from the real one."""
    path.write_text(
        json.dumps({"version": 1, "corpus_path": _corpus_path(), "decisions": decisions}),
        encoding="utf-8",
    )
    return path


def _write_record(tmp_path: Path, name: str, decision_id: str) -> Path:
    """Write the decision document an entry must be backed by."""
    record = tmp_path / name
    record.write_text(
        f"Decision {decision_id} records why this import was approved.\n", encoding="utf-8"
    )
    return record


# ------------------------------------------------------------------- controls


def test_sweep_reports_every_shape_a_corpus_read_takes(tmp_path: Path) -> None:
    """The probe fires on every importer shape, so an absence claim is safe."""
    reference, mik = _corpus_tail()
    root = _control_tree(
        tmp_path,
        {
            "apps/literal.py": f'ROOT = "{reference}/{mik}/20260908"\n',
            "apps/joined.py": _imports_the_corpus(),
            "apps/ospath.py": f'ROOT = os.path.join(DATA_DIR, "{reference}", "{mik}")\n',
            "apps/backslash.py": f'ROOT = "data\\\\{reference}\\\\{mik}"\n',
            "apps/handle.py": (
                "from apps.mik.reference_guard import corpus_root\nROOT = corpus_root()\n"
            ),
        },
    )
    assert set(_corpus_namers(root)) == {
        "apps/literal.py",
        "apps/joined.py",
        "apps/ospath.py",
        "apps/backslash.py",
        "apps/handle.py",
    }, f"a corpus read went unreported, so the sweep cannot see that shape: {_corpus_namers(root)}"


def test_sweep_stays_silent_on_prose_and_sibling_paths(tmp_path: Path) -> None:
    """The negative control: no report for text that is not a corpus read.

    The docstring here holds the corpus path VERBATIM, so removing the docstring
    exemption makes this test fail. A prose sample that could not trip the
    scanner with the exemption removed would assert nothing.
    """
    reference, mik = _corpus_tail()
    root = _control_tree(
        tmp_path,
        {
            "apps/prose.py": (
                f'"""Reads {_corpus_path()}/20260908/ for reference only."""\nROOT = 1\n'
            ),
            "apps/sibling.py": f'ROOT = DATA_DIR / "{reference}" / "{mik}store"\n',
            "apps/rekordbox_side.py": f'ROOT = DATA_DIR / "{reference}" / "rekordbox"\n',
            "apps/bare_segment.py": f'WORD = "{reference}"\nOTHER = "{mik}"\n',
        },
    )
    assert _corpus_namers(root) == {}


def test_sweep_fails_on_a_tree_carrying_an_unregistered_importer(tmp_path: Path) -> None:
    """The guard's own end to end: put an importer there and the sweep must say so."""
    registry = _write_registry(tmp_path / "registry.json", [])
    root = _control_tree(
        tmp_path / "tree", {"apps/imports_without_asking.py": _imports_the_corpus()}
    )
    assert "apps/imports_without_asking.py" in _unregistered_namers(root, registry), (
        "the sweep passed a tree that holds an unflagged importer for the corpus"
    )


def test_sweep_clears_the_same_tree_once_the_importer_is_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the control: the decision is what changes the answer."""
    root = _control_tree(tmp_path / "tree", {"apps/importer.py": _imports_the_corpus()})
    _write_record(tmp_path, "decision.md", "MIK-IMPORT-01")
    registry = _write_registry(
        tmp_path / "registry.json",
        [
            {
                "module": "apps/importer.py",
                "action": "import",
                "decision": "MIK-IMPORT-01",
                "record": "decision.md",
            }
        ],
    )
    monkeypatch.setattr(reference_guard, "REPO_ROOT", tmp_path)
    assert _unregistered_namers(root, registry) == {}


def test_a_decision_for_a_module_that_left_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decision for a module that no longer reads the corpus is an exemption, not a record."""
    root = _control_tree(tmp_path / "tree", {"apps/elsewhere.py": "ROOT = 1\n"})
    _write_record(tmp_path, "decision.md", "MIK-IMPORT-03")
    registry = _write_registry(
        tmp_path / "registry.json",
        [
            {
                "module": "apps/left_the_building.py",
                "action": "import",
                "decision": "MIK-IMPORT-03",
                "record": "decision.md",
            }
        ],
    )
    monkeypatch.setattr(reference_guard, "REPO_ROOT", tmp_path)
    assert _stale_decisions(root, registry) == ["apps/left_the_building.py"]


# ------------------------------------------------------------------ the sweep


def test_no_module_names_the_corpus_without_a_decision() -> None:
    """The guard CI runs: every tree that names the corpus is decided or declared."""
    offenders = _unregistered_namers(REPO_ROOT, reference_guard.DECISION_REGISTRY)
    assert not offenders, (
        "these modules reach for the Mixed In Key reference corpus without its own "
        f"decision: {offenders}. The corpus is reference data (NATIVE-12, D7): to import "
        "from it, record the decision in apps/mik/import_decisions.json with the document "
        "that holds it, then call require_mik_import_decision. To name it without reading "
        "it, add the module to DEFINITION_SITES with the reason."
    )
    assert _stale_decisions(REPO_ROOT, reference_guard.DECISION_REGISTRY) == []


def test_definition_sites_are_real_and_not_stale() -> None:
    """An exemption whose file is gone, or never named the corpus, is rot."""
    stale = [
        module
        for module in DEFINITION_SITES
        if not (REPO_ROOT / module).is_file() or module not in _corpus_namers(REPO_ROOT)
    ]
    assert not stale, f"DEFINITION_SITES entries that no longer name the corpus: {stale}"


def test_every_decision_is_backed_by_a_record_document(tmp_path: Path) -> None:
    """A decision with no document behind it is a name, not a decision."""
    registry = _write_registry(
        tmp_path / "registry.json",
        [
            {
                "module": "apps/importer.py",
                "action": "import",
                "decision": "MIK-IMPORT-99",
                "record": "gone.md",
            }
        ],
    )
    with pytest.raises(reference_guard.MikImportDecisionUnrecorded) as excinfo:
        reference_guard.load_import_decisions(registry)
    assert "MIK-IMPORT-99" in str(excinfo.value)


def test_no_import_decision_is_recorded_today() -> None:
    """The snapshot NATIVE-12 pins: nothing from the corpus has been imported.

    Flipping this IS the deliberate act the requirement asks for. Do it only with
    a recorded decision, and update this assertion in the same commit.
    """
    assert reference_guard.load_import_decisions() == (), (
        "an import decision was recorded. That is allowed only with its own recorded "
        "decision document (NATIVE-12); name it in this test so the change cannot land "
        "silently."
    )


# ----------------------------------------------------------- the refusal itself


def test_guard_refuses_an_unregistered_module() -> None:
    """Nothing is registered, so the flag refuses every caller today."""
    with pytest.raises(reference_guard.MikReferenceImportRefused) as excinfo:
        reference_guard.require_mik_import_decision("apps/mik/load.py", action="import")
    message = str(excinfo.value)
    assert "apps/mik/load.py" in message
    assert "import_decisions.json" in message


def test_guard_admits_a_registered_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Once a decision exists, the named module passes and an unnamed one does not."""
    _write_record(tmp_path, "decision.md", "MIK-IMPORT-02")
    registry = _write_registry(
        tmp_path / "registry.json",
        [
            {
                "module": "apps/mik/load.py",
                "action": "import",
                "decision": "MIK-IMPORT-02",
                "record": "decision.md",
            }
        ],
    )
    monkeypatch.setattr(reference_guard, "REPO_ROOT", tmp_path)
    granted = reference_guard.require_mik_import_decision(
        "apps/mik/load.py", action="import", registry_path=registry
    )
    assert granted.decision == "MIK-IMPORT-02"
    with pytest.raises(reference_guard.MikReferenceImportRefused):
        reference_guard.require_mik_import_decision(
            "apps/mik/match.py", action="import", registry_path=registry
        )
    with pytest.raises(reference_guard.MikReferenceImportRefused) as excinfo:
        reference_guard.require_mik_import_decision(
            "apps/mik/load.py", action="read", registry_path=registry
        )
    assert "import" in str(excinfo.value)


def test_guard_rejects_an_unreadable_registry(tmp_path: Path) -> None:
    """A missing registry must fail loudly, never read as 'nothing is registered, carry on'."""
    with pytest.raises(FileNotFoundError):
        reference_guard.load_import_decisions(tmp_path / "absent.json")
