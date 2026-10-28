"""import-linter adapter for the module public-surface rule (ADR-NEW-module-contracts).

Registered in `.importlinter` as contract type `public_surface`. All the logic is in
`scripts.arch_surface`, which the main test suite covers without import-linter; this
file only reads the graph and prints what the pure function returns. It runs inside the
throwaway environment `ops/quality/requirements.txt` pins, so it imports nothing beyond
import-linter, grimp and the standard library.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from grimp import ImportGraph
from importlinter.application import output
from importlinter.domain import fields
from importlinter.domain.contract import Contract, ContractCheck

from scripts.arch_surface import (
    CONTAINER,
    DEFAULT_ALLOWED_EXTERNAL,
    Violation,
    call_loads,
    pure_scope_imports,
    statement_imports,
    surface_violations,
    unread_modules,
)

REPO = Path(__file__).resolve().parents[1]


class PublicSurfaceContract(Contract):
    enforced = fields.ListField(subfield=fields.StringField())
    delivery = fields.ListField(subfield=fields.StringField())
    allowed_external = fields.ListField(subfield=fields.StringField(), required=False)
    dynamic_loaders = fields.ListField(subfield=fields.StringField(), required=False)
    composition_roots = fields.ListField(subfield=fields.StringField())  # required: no hidden default

    def check(self, graph: ImportGraph, verbose: bool) -> ContractCheck:  # noqa: ARG002 (import-linter API)
        # import-linter replaces each field with its parsed value before check() runs.
        enforced = cast("list[str]", self.enforced)
        delivery = cast("list[str]", self.delivery)
        configured = cast("list[str] | None", self.allowed_external)
        # Only an absent setting takes the default: an empty one allows no external package.
        allowed = sorted(DEFAULT_ALLOWED_EXTERNAL) if configured is None else configured
        loaders = cast("list[str] | None", self.dynamic_loaders) or []
        composition = cast("list[str]", self.composition_roots)
        missing = [p for p in enforced if f"{CONTAINER}.{p}" not in graph.modules]
        if missing:
            # A name that is not in the graph would be "enforced" over nothing and pass.
            raise ValueError(f"public_surface: enforced packages not found under {CONTAINER}/: {missing}")
        unknown = [m for m in loaders if m not in graph.modules]
        if unknown:
            raise ValueError(f"public_surface: dynamic_loaders not found in the graph: {unknown}")
        pairs = [
            (importer, imported)
            for importer in graph.modules
            for imported in graph.find_modules_directly_imported_by(importer)
        ]
        # The graph drops `if TYPE_CHECKING:` imports (exclude_type_checking_imports), so the
        # roots and contracts are also read from source, where those imports are kept, and
        # so is every module's every import statement, however guarded (a type-only deep import
        # still couples), and every call naming a module, which the graph never sees.
        source_pairs = pure_scope_imports(REPO, enforced, graph.modules)
        for found in (statement_imports(REPO, graph.modules), call_loads(REPO, graph.modules)):
            for pair, lines in found.items():
                source_pairs.setdefault(pair, []).extend(lines)
        violations = surface_violations(
            [*pairs, *source_pairs], enforced, delivery, allowed, dynamic_loaders=loaders, composition_roots=composition
        )
        # A compiled module's own imports are in no graph and no source: it is refused outright,
        # under every root the graph holds (each one unsquashed; external packages are squashed).
        roots = sorted(m for m in graph.modules if "." not in m and not graph.is_module_squashed(m))
        if CONTAINER not in roots:
            raise ValueError(f"public_surface: the graph holds no {CONTAINER} root: {roots}")
        violations += [Violation("unread-module", module, file) for module, file in unread_modules(REPO, roots)]
        details = []
        for v in violations:
            known = v.imported in graph.modules  # a dynamic import is read from source only
            details_found = graph.get_import_details(importer=v.importer, imported=v.imported) if known else []
            seen = {d["line_number"] for d in details_found}
            seen.update(source_pairs.get((v.importer, v.imported), []))
            lines = sorted(seen)
            details.append({"text": v.describe(), "lines": lines})
        return ContractCheck(kept=not violations, metadata={"violations": details, "checked": len(enforced)})

    def render_broken_contract(self, check: ContractCheck) -> None:
        for item in check.metadata["violations"]:
            where = ", ".join(f"l.{n}" for n in item["lines"]) or "line unknown"
            output.print_error(f"-   {item['text']} ({where})", bold=False)
        output.new_line()
