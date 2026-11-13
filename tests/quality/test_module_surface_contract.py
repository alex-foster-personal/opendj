"""The module public-surface rule (ADR-NEW-module-contracts) bites, and covers every new package.

`scripts.arch_surface.surface_violations` is the whole rule; `.importlinter` runs it through
`scripts.arch_surface_contract` on the real graph. These tests hold three things:

1. the rule fires on a deep import and on an impure contract, and stays quiet on the imports
   the ADR allows (each allowance has a control that breaks if the rule overshoots);
2. `.importlinter` registers the contract type and names real packages;
3. every package under apps/ is either enforced or in the frozen list of packages that
   predate the ADR, so a package added later is enforced from its first commit; a directory
   holding Python without an __init__.py (a namespace package) is refused unless named, and
   a top-level module (apps/foo.py) is refused outright.

[if] a package is added under apps/ and not to `enforced` [then] fail here, [else stop].

Implementation split across sibling modules in #10941; import this module for the frozen sets.
"""

from tests.quality.module_surface_contract_support import (
    COMPOSITION_ROOTS,
    CONTRACTS,
    DELIVERY,
    FIXTURES,
    LOOSE_BOUNDARIES,
    NOT_PACKAGES,
    OWED_CONTRACT,
    PREDATES_ADR,
    REPO,
    ROOT_MODULES,
    ROOT_NOT_PACKAGES,
    SECTION,
    _TC_CONTRACT,
    _check,
    _contract_packages,
    _enforced,
    _findings,
    _fixture,
    _outside_graph_violations,
    _packages,
    _section,
)

__all__ = [
    "COMPOSITION_ROOTS",
    "CONTRACTS",
    "DELIVERY",
    "FIXTURES",
    "LOOSE_BOUNDARIES",
    "NOT_PACKAGES",
    "OWED_CONTRACT",
    "PREDATES_ADR",
    "REPO",
    "ROOT_MODULES",
    "ROOT_NOT_PACKAGES",
    "SECTION",
    "_TC_CONTRACT",
    "_check",
    "_contract_packages",
    "_enforced",
    "_findings",
    "_fixture",
    "_outside_graph_violations",
    "_packages",
    "_section",
]
