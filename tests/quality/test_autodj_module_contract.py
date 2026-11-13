"""Dedicated guard for ``apps.autodj`` public module boundaries (issue #13264).

Outside ``apps.autodj``, production code may reach the package only through
``apps.autodj.contract`` or ``apps.autodj.http``, and every imported name must
appear on that surface's ``__all__``. Tests may keep deep planner imports (#7681).

[if] a supported external autodj import is used [then] only contract or http apply.
[if] an internal autodj module is imported externally [then] fail.
[if] a new public symbol is exported [then] the frozen ``__all__`` ratchet must move.
"""

from __future__ import annotations

import ast
import configparser
import os
import py_compile
from pathlib import Path

import pytest

from scripts.arch_contract_schema import declaration_problems
from scripts.arch_surface import source_imports, surface_violations
from tests.quality.test_module_surface_contract import (
    COMPOSITION_ROOTS,
    DELIVERY,
    OWED_CONTRACT,
    REPO,
)

SECTION = "importlinter:contract:module-public-surface"
AUTODJ_PUBLIC = frozenset({"contract", "http"})

CONTRACT_ALL: tuple[str, ...] = (
    "ANCHOR_LEAD_BARS",
    "AutoDjService",
    "BassEntry",
    "CONTRACT_VERSION",
    "ChooserCandidate",
    "ENERGY_UP",
    "HotCue",
    "INTENTS",
    "KeySegment",
    "PairNotFound",
    "PairTransition",
    "PlanError",
    "PlanEvent",
    "ProfileError",
    "RenderLayout",
    "RenderPlanError",
    "SHOWY",
    "SMOOTH",
    "STYLE_CAP",
    "Section",
    "Side",
    "StyleBars",
    "StyleParamsWire",
    "StyleSummary",
    "TrackProfile",
    "TransitionPlan",
    "VARIETY_WINDOW",
    "VOCALS_CLEAN",
    "ValidationResult",
    "WIND_DOWN",
)

HTTP_ALL: tuple[str, ...] = (
    "ANCHOR_LEAD_BARS",
    "ClosedMix",
    "Decision",
    "MirrorError",
    "MixWindow",
    "PairNotFound",
    "PairTransition",
    "PlanError",
    "ProfileError",
    "RENDER_SAMPLE_RATE",
    "ROLES",
    "RenderPlanError",
    "ScoreError",
    "SetPlan",
    "SimplifyError",
    "TRY_PICKS_SCHEMA_SOURCE",
    "TrackProfile",
    "TransitionCapture",
    "TransitionPlan",
    "Validation",
    "build_service",
    "check_mirror",
    "choose_for_set",
    "clear_pair_transition",
    "compile_plan",
    "compile_render_plan",
    "control_command",
    "decide",
    "downbeats_of",
    "effective_bars",
    "first_dispatch_bar",
    "get_pair_transition",
    "heard_on_deck",
    "heard_profile",
    "in_bars",
    "last_decision",
    "layout_of",
    "lead_short",
    "live_bar",
    "live_child",
    "live_mapping",
    "looks_like_score",
    "loop_engaged",
    "match_live_trim_db",
    "mix_windows",
    "next_transition",
    "on_decks",
    "parse_intent",
    "parse_plan",
    "parse_set",
    "plan_set",
    "print_score",
    "print_set_score",
    "profile_from_payloads",
    "propose_parity_bytes",
    "propose_transition",
    "read_set",
    "record_played",
    "refresh_proposal_confidence",
    "render_grid",
    "renders_at_start",
    "save_decision",
    "save_pair_transition",
    "score_document",
    "score_pair",
    "score_windows",
    "set_verdict",
    "simplify",
    "snap_to_downbeat",
    "try_picks",
    "validate_plan",
    "window_at",
    "with_in_track",
)


def _contracts() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(REPO / ".importlinter", encoding="utf-8")
    return parser


def _enforced() -> set[str]:
    return set(_contracts()[SECTION]["enforced"].split())


def _list_assign(path: Path, name: str) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                if isinstance(node.value, ast.List):
                    names = [
                        elt.value
                        for elt in node.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    ]
                    return tuple(sorted(names))
                msg = f"{path}: {name} is not a list of string constants"
                raise AssertionError(msg)
    msg = f"{path}: no {name} assignment"
    raise AssertionError(msg)


def _str_assign(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id == name
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                return node.value.value
    msg = f"{path}: no {name} string assignment"
    raise AssertionError(msg)


def _module_id(path: Path) -> tuple[str, bool]:
    rel = path.relative_to(REPO)
    parts = rel.with_suffix("").parts
    is_package = parts[-1] == "__init__"
    module = ".".join(parts[:-1] if is_package else parts)
    return module, is_package


def _known_modules() -> frozenset[str]:
    known: set[str] = set()
    apps = REPO / "apps"
    for root, dirs, files in os.walk(apps):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", "node_modules"} and d.isidentifier()]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = Path(root) / fname
            module, _ = _module_id(path)
            known.add(module)
    return frozenset(known)


def _production_sources() -> list[Path]:
    found: list[Path] = []
    for base in (REPO / "apps", REPO / "scripts"):
        for path in sorted(base.rglob("*.py")):
            if "node_modules" in path.parts:
                continue
            rel = path.relative_to(REPO)
            if rel.parts[0] == "apps" and len(rel.parts) >= 2 and rel.parts[1] == "autodj":
                continue
            found.append(path)
    return found


def _import_records_from_source(consumer: str, source: str) -> list[tuple[str, int, str, str | None]]:
    """(consumer module, line, imported module, optional symbol from ``from`` import)."""
    records: list[tuple[str, int, str, str | None]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            records.extend(
                (consumer, node.lineno, alias.name, None)
                for alias in node.names
                if alias.name == "apps.autodj" or alias.name.startswith("apps.autodj.")
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and (node.module == "apps.autodj" or node.module.startswith("apps.autodj."))
        ):
            records.extend((consumer, node.lineno, node.module, alias.name) for alias in node.names)
    return records


def _autodj_import_records() -> list[tuple[str, int, str, str | None]]:
    """(consumer module, line, imported module, optional symbol from ``from`` import)."""
    records: list[tuple[str, int, str, str | None]] = []
    for path in _production_sources():
        module, _ = _module_id(path)
        source = path.read_text(encoding="utf-8")
        records.extend(_import_records_from_source(module, source))
    return records


def _deep_import_offenders(
    records: list[tuple[str, int, str, str | None]],
) -> list[str]:
    offenders: list[str] = []
    for consumer, line, imported, _sym in records:
        parts = imported.split(".")
        if len(parts) < 3:
            offenders.append(f"{consumer}:{line} imports bare {imported}")
            continue
        if parts[0] != "apps" or parts[1] != "autodj":
            continue
        if len(parts) == 3 and parts[2] in AUTODJ_PUBLIC:
            continue
        if len(parts) > 3 and parts[2] in AUTODJ_PUBLIC:
            offenders.append(f"{consumer}:{line} deep-imports {imported} (use apps.autodj.{parts[2]} root only)")
            continue
        offenders.append(f"{consumer}:{line} deep-imports {imported} (only contract or http allowed)")
    return offenders


def _symbol_surface_offenders(
    records: list[tuple[str, int, str, str | None]],
) -> list[str]:
    contract_names = set(CONTRACT_ALL)
    http_names = set(HTTP_ALL)
    offenders: list[str] = []
    for consumer, line, imported, sym in records:
        if sym is None:
            continue
        if imported == "apps.autodj.contract":
            if sym not in contract_names:
                offenders.append(f"{consumer}:{line} imports undeclared contract name {sym!r}")
        elif imported == "apps.autodj.http":
            if sym not in http_names:
                offenders.append(f"{consumer}:{line} imports undeclared http name {sym!r}")
        elif imported.startswith("apps.autodj.contract."):
            offenders.append(f"{consumer}:{line} imports {imported}.{sym}; use apps.autodj.contract root")
    return offenders


def _autodj_import_violations(
    records: list[tuple[str, int, str, str | None]],
) -> list[str]:
    """Deep-import and undeclared-name rules shared by production scan and negative controls."""
    return _deep_import_offenders(records) + _symbol_surface_offenders(records)


def test_autodj_is_enforced_not_owed() -> None:
    assert "autodj" in _enforced()
    assert "autodj" not in OWED_CONTRACT


def test_contract_declaration() -> None:
    init = REPO / "apps/autodj/contract/__init__.py"
    assert _str_assign(init, "CONTRACT_VERSION") == "autodj/2"
    assert declaration_problems("autodj") == []
    assert _list_assign(init, "__all__") == CONTRACT_ALL


def test_http_barrel_declaration() -> None:
    http = REPO / "apps/autodj/http.py"
    assert _list_assign(http, "__all__") == HTTP_ALL
    py_compile.compile(str(http), doraise=True)


def test_no_deep_imports_outside_autodj() -> None:
    all_offenders = _autodj_import_violations(_autodj_import_records())
    offenders = [m for m in all_offenders if "deep-import" in m or "imports bare" in m]
    assert offenders == []


def test_external_names_are_declared_on_surface() -> None:
    all_offenders = _autodj_import_violations(_autodj_import_records())
    offenders = [m for m in all_offenders if "undeclared" in m or "use apps.autodj.contract root" in m]
    assert offenders == []


def test_production_autodj_surface_violations_empty() -> None:
    known = _known_modules()
    pairs: list[tuple[str, str]] = []
    for path in _production_sources():
        module, is_package = _module_id(path)
        source = path.read_text(encoding="utf-8")
        pairs.extend(
            (module, imported)
            for imported, _line in source_imports(source, module, is_package, known)
            if imported.startswith("apps.autodj")
        )
    violations = surface_violations(pairs, ("autodj",), DELIVERY, composition_roots=COMPOSITION_ROOTS)
    assert violations == []


@pytest.mark.parametrize(
    ("snippet", "expect_substrings"),
    [
        (
            "from apps.autodj.plan import parse_plan\n",
            ("apps.autodj.plan", "only contract or http"),
        ),
        (
            "from apps.autodj.contract.models import TrackProfile\n",
            ("apps.autodj.contract.models", "contract root"),
        ),
    ],
)
def test_control_snippets_fail_the_surface_rules(snippet: str, expect_substrings: tuple[str, ...]) -> None:
    consumer = "scripts.__autodj_boundary_control__"
    records = _import_records_from_source(consumer, snippet)
    offenders = _autodj_import_violations(records)
    assert offenders, "control snippet should be rejected"
    joined = " ".join(offenders)
    for piece in expect_substrings:
        assert piece in joined or piece in snippet
