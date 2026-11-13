"""Reading Python sources and the package imports they name, for scripts/ci_plan.py.

Split out of ci_plan.py, which crossed the file size ceiling. The division is real rather
than clerical: everything here answers "what does this FILE say", from bytes on disk up to
the set of packages a module imports, and knows nothing about scopes, verdicts or plans.
ci_plan.py owns that half and imports this one, never the reverse.

Every read here fails LOUD. A source this module cannot decode or parse is a dependency the
planner cannot see, and a planner that cannot see a dependency narrows around it.
"""

from __future__ import annotations

from scripts.ci_plan_sources_helper_loads import HelperLoads, loaded_test_helpers
from scripts.ci_plan_sources_literals import (
    _constant_segment,
    _directory_of,
    _join_parts,
    _simple_bindings,
    _single_bindings,
    joined_paths,
    mentioned_strings,
)
from scripts.ci_plan_sources_loader_graph import (
    LOADERS,
    NAME_ARGUMENT,
    NAME_LOADERS,
    PATH_ARGUMENT,
    PATH_JOINS,
    PATH_WRAPPERS,
    PLUGIN_MUTATIONS,
    PREPARES_ONLY,
    PROVIDERS,
    RUNS_SOURCE,
    SAME_PATH_METHODS,
    _argument,
    _called_name,
)
from scripts.ci_plan_sources_read import (
    PlanError,
    _imported_names,
    _read_source,
    _relative_base,
    imported_modules,
    imported_packages,
    imported_test_helpers,
    is_test_module,
    pytest_inputs,
    test_modules,
)
