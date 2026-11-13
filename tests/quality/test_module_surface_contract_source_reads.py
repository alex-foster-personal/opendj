"""Split from tests/quality/test_module_surface_contract.py (#10941; behaviour unchanged).

Type-checking imports, pure-scope dynamics, and loads through pydoc.
"""

from __future__ import annotations

import configparser
import importlib
import importlib.machinery
import os
import py_compile
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.arch_contract_schema import declaration_problems
from scripts.arch_surface import (
    DYNAMIC,
    Violation,
    call_loads,
    compiled_modules,
    loaded_imports,
    package_of,
    pure_scope_imports,
    source_imports,
    statement_imports,
    surface_violations,
    unread_modules,
    unread_root_modules,
)

from tests.quality import module_surface_contract_support as sup


_check = sup._check
_section = sup._section
_enforced = sup._enforced
_packages = sup._packages
_tree = sup._tree
_findings = sup._findings
_fixture = sup._fixture
_contract_packages = sup._contract_packages
_dotted = sup._dotted
_hold_python = sup._holds_python
_importable = sup._importable
_module_name = sup._module_name
_namespace_dirs = sup._namespace_dirs
_outside_graph_sources = sup._outside_graph_sources
_outside_graph_violations = sup._outside_graph_violations
_python_sources = sup._python_sources
_top_level_modules = sup._top_level_modules
_TC_CONTRACT = sup._TC_CONTRACT
_DYNAMIC_ROOT = sup._DYNAMIC_ROOT
REPO = sup.REPO
CONTRACTS = sup.CONTRACTS
SECTION = sup.SECTION
DELIVERY = sup.DELIVERY
COMPOSITION_ROOTS = sup.COMPOSITION_ROOTS
PREDATES_ADR = sup.PREDATES_ADR
OWED_CONTRACT = sup.OWED_CONTRACT
NOT_PACKAGES = sup.NOT_PACKAGES
ROOT_NOT_PACKAGES = sup.ROOT_NOT_PACKAGES
ROOT_MODULES = sup.ROOT_MODULES
FIXTURES = sup.FIXTURES
LOOSE_BOUNDARIES = sup.LOOSE_BOUNDARIES


def test_source_imports_keep_type_checking_imports_and_resolve_relative_ones() -> None:
    known = {"apps.lib.contract.ids", "apps.lib.store", "apps.lib.service"}
    got = {m for m, _ in source_imports(_TC_CONTRACT, "apps.lib.contract", True, known)}
    assert {"apps.lib.store", "apps.lib.service", "apps.lib.contract.ids", "pydantic", "typing"} <= got


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return tmp_path


def test_a_type_checking_import_in_a_contract_is_impure(tmp_path: Path) -> None:
    repo = _tree(tmp_path, {"apps/lib/__init__.py": "", "apps/lib/contract/__init__.py": _TC_CONTRACT})
    pairs = pure_scope_imports(repo, ["lib"], {"apps.lib.contract.ids", "apps.lib.store", "apps.lib.service"})
    found = {(v.importer, v.imported) for v in _check(*pairs)}
    assert found == {("apps.lib.contract", "apps.lib.store"), ("apps.lib.contract", "apps.lib.service")}
    assert pairs[("apps.lib.contract", "apps.lib.store")] == [7]


def test_type_checking_imports_outside_the_contract_are_not_read(tmp_path: Path) -> None:
    # Overshoot control: the implementation may annotate with anything it likes.
    repo = _tree(tmp_path, {"apps/lib/__init__.py": "", "apps/lib/service.py": _TC_CONTRACT})
    assert set(pure_scope_imports(repo, ["lib"], set())) == set()


_DYNAMIC_ROOT = """
import importlib
from importlib import import_module
importlib.import_module("apps.lib.store")
import_module(NAME)
__import__("apps.other.impl")
import_module(".service", __package__)
SPEC.loader.exec_module(MOD)
importlib.import_module("json")
from importlib import import_module as load
load("apps.other.alias")
fetch = importlib.import_module
fetch("apps.other.bound")
again = fetch
again("apps.other.again")
print("apps.other.plain")
import runpy
runpy.run_module("apps.other.ran")
runpy.run_path("apps/other/impl.py")
print(NAME)
F = getattr(LIB, "import_module")
getattr(LIB, LOADER)(NAME)
LIB.__dict__[LOADER](NAME)
B = __builtins__
exec(TEXT)
runpy.run_path("/opt/apps/other/impl.py")
(fetched := __import__)("apps.other.walrus")
fetched("apps.other.rebound")
(peek := getattr(LIB, LOADER))(NAME)
import pkgutil
pkgutil.resolve_name("apps.other.resolved:main")
(LOAD if FLAG else OTHER)(NAME)
def load_with(loader):
    return loader("apps.other.forwarded")
load_with(__import__)
run = exec
ev = builtins.eval
"""


def test_a_dynamic_import_in_a_pure_scope_is_impure(tmp_path: Path) -> None:
    # Codex, PR #6182: neither the graph nor the AST reader saw a module loaded by a call.
    repo = _tree(tmp_path, {"apps/lib/__init__.py": _DYNAMIC_ROOT, "apps/lib/contract/__init__.py": ""})
    pairs = pure_scope_imports(repo, ["lib"], set())
    found = {(v.imported, *pairs[(v.importer, v.imported)]) for v in _check(*pairs)}
    # Lines 11, 13 and 15 call a loader through a name the module bound it to (Codex, PR #6182).
    assert found == {
        ("apps.lib.store", 4),
        ("apps.other.impl", 6, 19),  # 19 runs it by its repository path (Codex, PR #6182)
        ("apps.other.alias", 11),
        ("apps.other.bound", 13),
        ("apps.other.again", 15),  # bound to a name that was itself bound to a loader
        ("apps.other.ran", 18),  # runpy runs a module by name (Codex, PR #6182)
        # Codex, PR #6182: a loader bound by an assignment expression (27, 28), and pkgutil's
        # resolver, which imports the module before the colon (31)
        ("apps.other.walrus", 27),
        ("apps.other.rebound", 28),
        ("apps.other.resolved", 31),
        # 12, 14 and 35 hand a loader on rather than call it, so what it loads is not read (Codex, PR #6182);
        # 21-25, 29 and 32 reach a loader by reflection (32 through any callee but a name or attribute);
        # 26 runs a path outside the repository; 36 and 37 bind an evaluator to a name
        (DYNAMIC, 5, 7, 8, 12, 14, 21, 22, 23, 24, 25, 26, 29, 32, 35, 36, 37),
        # Sol, PR #6182: the loader machinery itself, through which reflection reaches a loader
        ("importlib", 2, 3, 10),
        ("runpy", 17),
        ("pkgutil", 30),
    }
    # Controls: a literal stdlib name (line 9) is read like `import json`, and so is pure;
    # a call that loads nothing (line 16) is not read as a load because it names a module.
    assert pairs[("apps.lib", "json")] == [9]
    assert next(v for v in _check(*pairs) if v.imported == DYNAMIC).describe() == (
        "apps.lib: loads a module by a name no gate can read; a contract imports statically"
    )


def test_a_module_held_only_compiled_is_read_as_itself(tmp_path: Path) -> None:
    # Codex, PR #6182: with only ``helper.pyd`` beside it, ``from . import helper`` read as an
    # import of the root itself, since the graph knows only modules it reads from source; Windows
    # runs the extension. Controls: a name with no file, bytecode under __pycache__, and a file
    # whose name is no module's stay the package; a consumer's load and type-only import read it too.
    root = "from . import helper\nfrom . import fast\nfrom . import cached\nfrom . import nothing\n"
    root += "from . import contract\nfrom . import odd\n"
    files = {
        "apps/lib/__init__.py": root,
        "apps/lib/contract/__init__.py": "",
        "apps/lib/helper.pyd": "",
        "apps/lib/fast.cpython-311-x86_64-linux-gnu.so": "",
        "apps/lib/__pycache__/cached.cpython-311.pyc": "",
        "apps/lib/odd-name.pyd": "",
        "apps/ui/__init__.py": "",
        "apps/ui/page.py": 'getattr(LIB, NAME)("apps.lib.helper")\nif TYPE_CHECKING:\n    from apps.lib import helper\n',
    }
    repo = _tree(tmp_path, files)
    assert compiled_modules(repo) == {"apps.lib.helper", "apps.lib.fast"}
    pairs = pure_scope_imports(repo, ["lib"], {"apps.lib.contract"})
    assert pairs == {
        ("apps.lib", "apps.lib.helper"): [1],
        ("apps.lib", "apps.lib.fast"): [2],
        ("apps.lib", "apps.lib"): [3, 4, 6],
        ("apps.lib", "apps.lib.contract"): [5],
    }
    found = {(v.importer, v.imported) for v in _check(*pairs)}
    assert found == {("apps.lib", "apps.lib.helper"), ("apps.lib", "apps.lib.fast")}
    consumer = {"apps.ui", "apps.ui.page"}
    assert loaded_imports(repo, consumer) == {("apps.ui.page", "apps.lib.helper"): [1]}
    assert statement_imports(repo, consumer) == {("apps.ui.page", "apps.lib.helper"): [3]}


def test_a_load_no_gate_can_read_fails_closed_outside_the_listed_loaders(tmp_path: Path) -> None:
    # Codex, PR #6182: each new way to build a load target (an alias of a wrapper, a spread into
    # it) read as an unresolved load, which the deep-import rule dropped, so the gate passed. An
    # unresolved load is now refused wherever it is, unless its module is a listed loader.
    consumer = (
        "import importlib\n"
        'def load(leaf):\n    importlib.import_module("apps.lib." + leaf)\n'
        'alias = load\nalias("impl")\nload(*("impl",))\n'
    )
    files = {"apps/ui/__init__.py": "", "apps/ui/page.py": consumer, "apps/ui/named.py": "import json\n"}
    known = {"apps.ui", "apps.ui.page", "apps.ui.named", "apps.lib.impl"}
    loads = call_loads(_tree(tmp_path, files), known)
    assert loads == {("apps.ui.page", DYNAMIC): [3]}
    assert loaded_imports(_tree(tmp_path, files), known) == {}  # the resolved view drops it
    pairs = [*loads, ("apps.lib", DYNAMIC), ("scripts.tool", DYNAMIC)]
    refused = Violation("dynamic-load", "apps.ui.page", DYNAMIC)
    # Codex, PR #6182: a script outside the container loads an implementation as surely as an
    # app does, so it is refused too. Controls: a pure scope's unresolved load is refused once,
    # as impure; a listed loader passes, in the container or out; a listed module that loads
    # nothing that way is stale, so the list only shrinks.
    impure = Violation("impure-contract", "apps.lib", DYNAMIC)
    script = Violation("dynamic-load", "scripts.tool", DYNAMIC)
    assert surface_violations(pairs, ["lib"], DELIVERY, composition_roots=COMPOSITION_ROOTS) == [
        refused,
        script,
        impure,
    ]
    listing = ["apps.ui.page", "apps.ui.named", "scripts.tool"]
    listed = surface_violations(pairs, ["lib"], DELIVERY, dynamic_loaders=listing, composition_roots=COMPOSITION_ROOTS)
    assert listed == [impure, Violation("stale-dynamic-loader", "apps.ui.named", DYNAMIC)]
    assert refused.describe() == (
        "apps.ui.page: loads a module by a name no gate can read; name the module in source,"
        " or list this module under dynamic_loaders"
    )


def test_code_run_by_a_qualified_or_aliased_evaluator_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``builtins.exec(code)`` ran text no reader sees, but only a bare ``exec``
    # call read as DYNAMIC. Controls: ``re.compile`` and ``model.eval()`` in a module that never
    # names builtins are no evaluator.
    files = {
        "apps/ui/__init__.py": "",
        "apps/ui/bare.py": "exec(code)\n",
        "apps/ui/qualified.py": "import builtins\nbuiltins.exec(code)\n",
        "apps/ui/aliased.py": "import builtins as b\nb.eval(code)\n",
        "apps/ui/imported.py": "from builtins import exec as run\nrun(code)\n",
        "apps/ui/namespace.py": "__builtins__.compile(code, 'x', 'exec')\n",
        "apps/ui/controls.py": "import re\nre.compile(pattern)\nmodel.eval()\nimport os as builtins_\nbuiltins_.exec(c)\n",
        # Codex, PR #6182: the builtins module held under a name a load returns, or taken from
        # the module table. Which name holds it is not followed: in a module that can hold it at
        # all, every attribute call named like an evaluator is one. Control: a module that
        # imports sys but never reaches its module table.
        "apps/ui/loaded.py": 'b = __import__("builtins")\nb.exec(code)\n',
        "apps/ui/table.py": "import sys\nm = sys.modules[k]\nm.eval(code)\n",
        "apps/ui/globals.py": 'g = f.__globals__["__builtins__"]\ng.exec(code)\n',
        "apps/ui/plain_sys.py": "import sys\nimport re\nsys.exit(re.compile(p))\nmodel.eval()\n",
        # Control: ``re`` holds only the re module, though this module reaches builtins; but a
        # name an import binds and something else rebinds may hold anything, and so may a chain.
        "apps/ui/regex.py": "import sys, re\nm = sys.modules\nre.compile(p)\n",
        "apps/ui/rebound.py": 'import sys, re\nm = sys.modules\nre = m["builtins"]\nre.eval(code)\n',
        "apps/ui/chained.py": 'import sys\nsys.modules["builtins"].exec(code)\n',
        "apps/ui/attr_chain.py": "import sys, os\nm = sys.modules\nos.held.exec(code)\n",
    }
    known = {"apps.ui", *(f"apps.ui.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair[0]: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        "apps.ui.bare": [1],
        "apps.ui.qualified": [2],
        "apps.ui.aliased": [2],
        "apps.ui.imported": [2],
        "apps.ui.namespace": [1],
        "apps.ui.loaded": [2],
        "apps.ui.table": [2, 3],  # the module table read by a key no reader folds, or handed on, too
        "apps.ui.globals": [2],
        "apps.ui.regex": [2],
        "apps.ui.rebound": [2, 4],
        "apps.ui.chained": [2],
        "apps.ui.attr_chain": [2, 3],
    }


def test_a_load_through_a_name_bound_only_to_text_is_read(tmp_path: Path) -> None:
    # Codex, PR #6182: ``target = "json"; import_module(target)`` inside a function names its
    # module as plainly as a literal, but read as DYNAMIC and was refused. Controls: a name also
    # bound some other way (reassigned from input, a loop target, a parameter, ``+=``) may hold
    # anything and stays DYNAMIC.
    files = {
        "apps/ui/__init__.py": "",
        "apps/ui/local.py": 'import importlib\ndef f():\n    target = "json"\n    importlib.import_module(target)\n',
        "apps/ui/deep.py": (
            "import importlib\n"
            'def f(on):\n    if on:\n        t = "apps.lib.impl"\n    else:\n        t = "json"\n'
            "    importlib.import_module(t)\n"
        ),
        "apps/ui/mixed.py": 'import importlib\nt = "json"\nt = input()\nimportlib.import_module(t)\n',
        "apps/ui/looped.py": 'import importlib\nfor t in names():\n    importlib.import_module(t)\nt = "json"\n',
        "apps/ui/param.py": "import importlib\ndef f(t):\n    importlib.import_module(t)\nt = 'json'\n",
        "apps/ui/grown.py": 'import importlib\nt = "apps"\nt += suffix\nimportlib.import_module(t)\n',
    }
    known = {"apps.ui", *(f"apps.ui.{Path(f).stem}" for f in files if "__init__" not in f), "apps.lib.impl"}
    loads = call_loads(_tree(tmp_path, files), known)
    # ``json`` is no module of the repository, so a load of it is no pair at all.
    assert {pair: lines for pair, lines in loads.items() if pair[0] in {"apps.ui.local", "apps.ui.deep"}} == {
        ("apps.ui.deep", "apps.lib.impl"): [7],
    }
    assert sorted(pair[0] for pair in loads if pair[1] == DYNAMIC) == [
        "apps.ui.grown",
        "apps.ui.looped",
        "apps.ui.mixed",
        "apps.ui.param",
    ]


def test_a_consumer_load_by_reflection_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``getattr(importlib, name)(target)`` reaches a loader by reflection, which
    # only the pure-scope reader refused; a consumer's read nothing. Controls: a computed callee
    # in a module that only reads attributes off a loader module (or off anything imported from
    # one), and a computed callee on a module no loader lives in, are no load. A dunder read off a
    # loader module (``b.__dict__[name]``) hands out its namespace, so it holds the module loosely;
    # a loader's own name (``b.__import__``) and a namespace of another module do not.
    files = {
        "apps/ui/__init__.py": "",
        "apps/ui/reflect.py": "import importlib\ngetattr(importlib, loader)(target)\n",
        "apps/ui/named.py": 'import importlib as il\nf = getattr(il, "import_module")\n',
        "apps/ui/handed.py": "import importlib\nload_with(importlib.import_module)\n",
        "apps/ui/table.py": 'import importlib\nimportlib.import_module("json")\nHANDLERS[kind](row)\n',
        "apps/ui/resources.py": 'from importlib import resources\nresources.files("x")\nTABLE[k]()\n',
        "apps/ui/ctypes_.py": 'import ctypes\ngetattr(ctypes, "WinDLL")("kernel32")\n',
        "apps/ui/loose.py": "from importlib import util\nkeep(util)\nTABLE[k](name)\n",
        # Codex, PR #6182: a loader module taken from a load or the module table, not an import.
        # Control: a module that names no loader module as text and never reaches the table.
        "apps/ui/loaded.py": 'm = __import__("importlib")\nm.__dict__[loader](target)\n',
        "apps/ui/tabled.py": "import sys\nm = sys.modules[k]\nTABLE[m](target)\n",
        "apps/ui/texted.py": 'import json\njson.loads("{}")\nTABLE[k](row)\n',
        "apps/ui/notsys.py": "import config\nconfig.modules\nTABLE[k](row)\n",
        "apps/ui/spaced.py": 'import builtins as b\nb.__dict__["__im" + "port__"](target)\n',
        "apps/ui/deep.py": "import importlib\nimportlib.util.__dict__[k](target)\n",
        "apps/ui/own.py": 'import builtins as b\nb.__import__("json")\nTABLE[k](row)\n',
        "apps/ui/other.py": "import json\njson.__dict__[k](row)\n",
    }
    known = {"apps.ui", *(f"apps.ui.{Path(f).stem}" for f in files if f.endswith(".py") and "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("apps.ui.reflect", DYNAMIC): [2],
        ("apps.ui.named", DYNAMIC): [2],
        ("apps.ui.handed", DYNAMIC): [2],
        ("apps.ui.loose", DYNAMIC): [2, 3],  # importlib.util handed on (`_machinery_handed`)
        ("apps.ui.spaced", DYNAMIC): [2],
        ("apps.ui.deep", DYNAMIC): [2],
        ("apps.ui.loaded", DYNAMIC): [2],
        ("apps.ui.tabled", DYNAMIC): [2, 3],
    }


def test_a_loader_name_only_tested_or_resolved_is_no_load(tmp_path: Path) -> None:
    # Sol, PR #6182: a gate's own table of loader names, and importlib.util.resolve_name (which
    # imports nothing, unlike pkgutil's), read as loads, so a module listed only for them kept its
    # dynamic_loaders entry alive. Controls: the same text handed to a lookup, a table read other
    # than by ``in``, pkgutil's resolve_name, resolve_name off a rebound or aliased importlib, and
    # another importlib.util member bound to a loader's name stay loads.
    files = {
        "scripts/__init__.py": "",
        "scripts/tested.py": (
            'LOADERS = frozenset({"import_module", "run_path"})\n'
            'def f(node):\n    return node.attr in LOADERS or node.attr == "exec_module" or node in {"load_module"}\n'
        ),
        "scripts/resolves.py": (
            "import importlib\nimport importlib.util\nfrom importlib.util import resolve_name as rn\n"
            "importlib.util.resolve_name(name, pkg)\nrn(name, pkg)\n"
        ),
        "scripts/iterated.py": 'NAMES = {"import_module"}\ndef f(m):\n    for n in NAMES:\n        getattr(m, n)(x)\n',
        "scripts/handed.py": 'def f(m):\n    return getattr(m, "import_module")\n',
        "scripts/copied.py": 'NAMES = {"import_module"}\nALL = set(NAMES)\n',
        "scripts/pkg.py": "import pkgutil\npkgutil.resolve_name(name)\n",
        "scripts/rebound.py": "import importlib.util\nimportlib = pkgutil\nimportlib.util.resolve_name(name, pkg)\n",
        "scripts/aliased.py": "import pkgutil as importlib\nimportlib.util.resolve_name(name, pkg)\n",
        "scripts/renamed.py": "from importlib.util import find_spec as load_module\nload_module(name)\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("scripts.iterated", DYNAMIC): [1],
        ("scripts.handed", DYNAMIC): [2],
        ("scripts.copied", DYNAMIC): [1],
        ("scripts.pkg", DYNAMIC): [2],
        ("scripts.rebound", DYNAMIC): [3],
        ("scripts.aliased", DYNAMIC): [2],
        ("scripts.renamed", DYNAMIC): [2],
    }
