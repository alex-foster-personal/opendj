"""NATIVE-12: the Mixed In Key reference corpus is reference data, not a source.

A full Mixed In Key run was captured on Tue 8 Sep 2026 and kept outside git
under ``<data-dir>/reference/mik/<date>/`` (D7, ``specs/native-analysis-v1.md``).
The corpus may be COMPARED against, but nothing from it may be imported into
audio files, the odj library or ``state.db`` without a decision of its own --
a separate, recorded, named one. That is not a fact about the corpus; it is a
property of every future importer, so it needs an enforcement point rather
than a note in a README.

Two halves, and neither works alone:

* :func:`require_mik_import_decision` is the runtime refusal. An importer passes
  its own module path and the action it is about to take; the call raises unless
  ``import_decisions.json`` holds a decision for that module AND the document
  the decision names is on disk and states the decision id. The registry is
  empty today, so every caller is refused by construction -- turning the corpus
  into a source is an edit to that file, in a diff somebody reviews, not a
  side effect of writing a loader.
* ``tests/mik/test_reference_import_guard.py`` is the static half: it sweeps the
  tree for modules that name the corpus (literal path, joined parts,
  ``os.path.join``, or the accessors below) and fails on any that is neither a
  decision holder nor a declared definition site. Without it the refusal is
  only ever called by modules that already chose to be honest.

This module hands out no path to read from. :func:`corpus_segments` states
where the corpus is so the sweep can classify a module that holds it, and the
sweep treats importing this module at all -- under any name, alias included --
as holding it. There is deliberately no accessor that returns the root: a
function that returned it would be the same unguarded door, one call deeper.

Regression lines:
  - if a caller can obtain the corpus root without a recorded decision then broken
  - if an unreadable registry is read as an empty one then broken
  - if a decision names a record document that does not exist then broken
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DECISION_REGISTRY: Path = Path(__file__).with_name("import_decisions.json")

# The two acts NATIVE-12 separates. "read" is comparing against the corpus in
# place; "import" is taking a value out of it into our own stores or files.
ALLOWED_ACTIONS: frozenset[str] = frozenset({"read", "import"})

REQUIRED_DECISION_FIELDS: tuple[str, ...] = ("module", "action", "decision", "record")


class MikReferenceImportRefused(RuntimeError):
    """A module reached for the reference corpus without a recorded decision."""


class MikImportDecisionUnrecorded(RuntimeError):
    """A registry entry names a decision document that is missing or does not hold it."""


@dataclass(frozen=True)
class MikImportDecision:
    """One allowed import: the module, the act, the decision id, the document."""

    module: str
    action: str
    decision: str
    record: str


@dataclass(frozen=True)
class _Registry:
    """The parsed registry: where the corpus is, and which imports are decided."""

    corpus_path: str
    decisions: tuple[dict[str, str], ...]


def corpus_segments(registry_path: Path = DECISION_REGISTRY) -> tuple[str, ...]:
    """The corpus location as path segments, from the registry that owns it.

    Kept as segments rather than a path so the static sweep can match a joined
    expression (``DATA_DIR / "reference" / "mik"``) without knowing which root
    the caller started from.
    """
    return tuple(_load_registry(registry_path).corpus_path.split("/"))


def load_import_decisions(
    registry_path: Path = DECISION_REGISTRY, record_root: Path = REPO_ROOT
) -> tuple[MikImportDecision, ...]:
    """Every recorded import decision, validated against its record document.

    ``record_root`` is where an entry's ``record`` document is resolved from.
    Production always passes the default; it is an argument rather than a
    module constant so a test can aim the check at a temporary tree without
    replacing production state.

    Raises :class:`FileNotFoundError` if the registry is absent and
    :class:`MikImportDecisionUnrecorded` if an entry's document is missing or
    does not name the decision. A registry that cannot be read is never read
    as an empty one: that would turn a broken guard into a permissive one.
    """
    decisions: list[MikImportDecision] = []
    for entry in _load_registry(registry_path).decisions:
        missing = [field for field in REQUIRED_DECISION_FIELDS if not entry.get(field)]
        if missing:
            raise MikImportDecisionUnrecorded(
                f"registry entry {entry!r} is missing {missing}; a decision needs a module, "
                "an action, a decision id and the document that records it"
            )
        if entry["action"] not in ALLOWED_ACTIONS:
            raise MikImportDecisionUnrecorded(
                f"registry entry {entry!r} names action {entry['action']!r}, "
                f"which is not one of {sorted(ALLOWED_ACTIONS)}"
            )
        decision = MikImportDecision(
            module=entry["module"],
            action=entry["action"],
            decision=entry["decision"],
            record=entry["record"],
        )
        decision_document_states_the_decision(decision, record_root)
        decisions.append(decision)
    pairs = [(decision.module, decision.action) for decision in decisions]
    duplicated = sorted({pair for pair in pairs if pairs.count(pair) > 1})
    if duplicated:
        raise MikImportDecisionUnrecorded(
            f"{registry_path} holds more than one decision for {duplicated}; one action is one "
            "decision, and two entries leave the grant ambiguous"
        )
    return tuple(decisions)


def find_import_decision(
    module: str,
    action: str,
    registry_path: Path = DECISION_REGISTRY,
    record_root: Path = REPO_ROOT,
) -> MikImportDecision | None:
    """The decision recorded for ``module`` and exactly ``action``, or None.

    Keyed on the pair, not on the module: a module decided for ``read`` and
    later for ``import`` holds two decisions, and a lookup that took the first
    match would answer whichever one the file happened to list first.
    """
    return _lookup(load_import_decisions(registry_path, record_root), module, action)


def _lookup(
    decisions: tuple[MikImportDecision, ...], module: str, action: str
) -> MikImportDecision | None:
    """The decision for ``module`` and ``action``, or None. At most one exists.

    ``load_import_decisions`` refuses a registry holding two entries for one
    pair, so this filter can return the first match without resolving an
    ambiguity by file order.
    """
    matches = [d for d in decisions if d.module == module and d.action == action]
    return matches[0] if matches else None


def require_mik_import_decision(
    module: str,
    *,
    action: str,
    registry_path: Path = DECISION_REGISTRY,
    record_root: Path = REPO_ROOT,
) -> MikImportDecision:
    """Refuse unless ``module`` holds a recorded decision for exactly ``action``.

    Returns the granted decision so a caller can log what it is acting under.
    Raises :class:`MikReferenceImportRefused` naming the module, the action,
    the registry to edit and the decision document to write.
    """
    if action not in ALLOWED_ACTIONS:
        raise MikReferenceImportRefused(
            f"unknown action {action!r}; NATIVE-12 separates {sorted(ALLOWED_ACTIONS)}"
        )
    decisions = load_import_decisions(registry_path, record_root)
    decision = _lookup(decisions, module, action)
    if decision is not None:
        return decision
    held = sorted({d.action for d in decisions if d.module == module})
    detail = (
        f"it holds a {held[0]!r} decision, which does not cover {action!r}"
        if held
        else "it has no recorded decision"
    )
    raise MikReferenceImportRefused(
        f"{module} may not {action} from the Mixed In Key reference corpus: {detail}. The "
        f"corpus is reference data only (NATIVE-12, D7). To import from it, add a decision for "
        f"this module and action to {registry_path.name} naming the document that records it, "
        "then call this guard at the read site."
    )


def decision_document_states_the_decision(
    decision: MikImportDecision, record_root: Path = REPO_ROOT
) -> Path:
    """The record document behind ``decision``, proven to name the decision id.

    The path must be relative and must resolve inside ``record_root``: an
    absolute path, or one climbing out with ``..``, would let a decision be
    "recorded" in a file nobody reviews.
    """
    declared = Path(decision.record)
    root = record_root.resolve()
    if declared.is_absolute():
        raise MikImportDecisionUnrecorded(
            f"{decision.module}: decision {decision.decision} names the absolute path "
            f"{decision.record}. A decision document lives inside the repository, where a "
            "reviewer can see it."
        )
    document = (root / declared).resolve()
    if not document.is_relative_to(root):
        raise MikImportDecisionUnrecorded(
            f"{decision.module}: decision {decision.decision} names {decision.record}, which "
            f"resolves outside {root}. A decision document lives inside the repository."
        )
    if not document.is_file():
        raise MikImportDecisionUnrecorded(
            f"{decision.module}: decision {decision.decision} names {decision.record}, "
            "which is not in the tree. A decision with no document is a name, not a decision."
        )
    if decision.decision not in document.read_text(encoding="utf-8"):
        raise MikImportDecisionUnrecorded(
            f"{decision.record} does not state {decision.decision}; the document must be the "
            "record of THIS decision, not a pointer at it"
        )
    return document


def _load_registry(registry_path: Path) -> _Registry:
    """Parse the registry, raising rather than defaulting when it is absent or malformed."""
    if not registry_path.is_file():
        raise FileNotFoundError(
            f"Mixed In Key import registry {registry_path} is missing. It is the record of "
            "which modules may import the reference corpus; a missing one cannot be read as "
            "'nothing is registered' (NATIVE-12)."
        )
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    corpus_path = payload.get("corpus_path")
    if not isinstance(corpus_path, str) or not corpus_path:
        raise MikImportDecisionUnrecorded(f"{registry_path} states no corpus_path")
    if "decisions" not in payload:
        raise MikImportDecisionUnrecorded(
            f"{registry_path} states no decisions list. A registry that lost the key is not the "
            "empty registry NATIVE-12 pins; reading it as one would turn corruption into a pass."
        )
    raw = payload["decisions"]
    if not isinstance(raw, list):
        raise MikImportDecisionUnrecorded(f"{registry_path} decisions is not a list")
    return _Registry(corpus_path=corpus_path, decisions=tuple(_string_fields(raw, registry_path)))


def _string_fields(raw: list[object], registry_path: Path) -> list[dict[str, str]]:
    """Every registry entry as a string-keyed, string-valued map, or a loud refusal."""
    entries: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise MikImportDecisionUnrecorded(
                f"{registry_path} has a non-object decision: {entry!r}"
            )
        fields: dict[str, str] = {}
        for key, value in entry.items():
            if not isinstance(value, str):
                raise MikImportDecisionUnrecorded(
                    f"{registry_path} decision field {key!r} is not a string: {value!r}"
                )
            fields[str(key)] = value
        entries.append(fields)
    return entries
