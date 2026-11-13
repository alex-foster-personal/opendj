"""Split from tests/quality/test_module_surface_contract.py (#10941; behaviour unchanged).

Compiled refusal, consumer deep imports, and pure roots on this tree.
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


def test_this_tree_lists_every_dynamic_loader_it_has() -> None:
    listed = _section()["dynamic_loaders"].split()
    assert listed, "no loader listed: the setting is read from the wrong place"
    assert all(m.split(".")[0] in {"apps", "scripts", "ops"} for m in listed)


def test_a_shipped_module_held_only_compiled_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Codex, PR #6182: a consumer held only as ``native.pyd`` imports what no gate reads, so it
    # could import any implementation unseen. Controls: build output git ignores never ships and
    # passes; a tracked file stays refused though an ignore rule matches it; where git cannot
    # answer (no repository here yet, or no git), nothing passes as ignored; a source module is never listed.
    files = {
        "apps/ui/__init__.py": "",
        "apps/ui/page.py": "",
        "apps/ui/native.pyd": "",
        "apps/ui/types.pyi": "import apps.dedup.impl\n",  # Codex, PR #6182: type-only coupling no graph reads
        "apps/ui/target/debug/built.so": "",
        "apps/ui/kept/tracked.so": "",
        "apps/ui/.gitignore": "/target/\n/kept/\n",
        "scripts/__init__.py": "",
        "scripts/tool.so": "",
    }
    repo = _tree(tmp_path, files)
    everything = [
        ("apps.ui.kept.tracked", "apps/ui/kept/tracked.so"),
        ("apps.ui.native", "apps/ui/native.pyd"),
        ("apps.ui.target.debug.built", "apps/ui/target/debug/built.so"),
        ("apps.ui.types", "apps/ui/types.pyi"),
    ]
    # Codex, PR #6182: every root the graph holds is scanned, ``scripts`` as well as ``apps``;
    # a root not asked for (the default asks for ``apps`` alone) is not.
    script = ("scripts.tool", "scripts/tool.so")
    assert unread_modules(repo, ["apps", "scripts"]) == sorted([*everything, script])
    assert unread_modules(repo) == everything
    git = shutil.which("git")
    assert git is not None, "git is what tells shipped files from build output"
    subprocess.run([git, "-C", str(repo), "init", "-q"], check=True)
    subprocess.run([git, "-C", str(repo), "add", "-f", "apps/ui/kept/tracked.so"], check=True)
    assert unread_modules(repo) == [everything[0], everything[1], everything[3]]
    with monkeypatch.context() as patched:  # no git to ask: nothing passes as ignored
        patched.setattr(shutil, "which", lambda _: None)
        assert unread_modules(repo) == everything
    assert (
        Violation("unread-module", *everything[1])
        .describe()
        .startswith("apps.ui.native: held only as apps/ui/native.pyd, which no gate can read")
    )


def test_this_tree_ships_no_module_held_only_compiled() -> None:
    parser = configparser.ConfigParser()
    parser.read(CONTRACTS)
    roots = parser["importlinter"]["root_packages"].split()
    assert "apps" in roots and len(roots) > 1, f"read the wrong roots: {roots}"
    assert unread_modules(REPO, roots) == []


def test_a_dynamic_import_outside_a_pure_scope_is_not_read(tmp_path: Path) -> None:
    # Overshoot control: the implementation may load what it likes.
    repo = _tree(tmp_path, {"apps/lib/__init__.py": "", "apps/lib/service.py": _DYNAMIC_ROOT})
    assert set(pure_scope_imports(repo, ["lib"], set())) == set()


_CONSUMER = """
from typing import TYPE_CHECKING
from apps.lib.impl import runtime_thing
if TYPE_CHECKING:
    from apps.lib.service import Impl
    from apps.lib.contract import Model
if FLAG:
    from apps.lib.other import runtime_too
from typing import TYPE_CHECKING as TC
if TC:
    from apps.lib.aliased import A
if TYPE_CHECKING and FLAG:
    from apps.lib.joined import J
if not TYPE_CHECKING:
    pass
else:
    from apps.lib.negated import N
"""


def test_a_type_only_deep_import_in_a_consumer_is_a_violation(tmp_path: Path) -> None:
    # Sol, PR #6182: a guard in any form (an alias, ``and``, an ``else``) is still a type-only
    # import the graph drops, so every import statement is read from source, however guarded;
    # the ones the graph keeps read the same pairs again. Control: a contract import is no violation.
    repo = _tree(tmp_path, {"apps/ui/__init__.py": "", "apps/ui/page.py": _CONSUMER})
    deep = ("impl", "service", "other", "aliased", "joined", "negated")
    known = {"apps.ui", "apps.ui.page", "apps.lib.contract", *(f"apps.lib.{name}" for name in deep)}
    pairs = statement_imports(repo, known)
    assert pairs == {
        ("apps.ui.page", "apps.lib.impl"): [3],
        ("apps.ui.page", "apps.lib.service"): [5],
        ("apps.ui.page", "apps.lib.contract"): [6],
        ("apps.ui.page", "apps.lib.other"): [8],
        ("apps.ui.page", "apps.lib.aliased"): [11],
        ("apps.ui.page", "apps.lib.joined"): [13],
        ("apps.ui.page", "apps.lib.negated"): [17],
        ("apps.ui.page", "typing"): [2, 9],
    }
    assert _check(*pairs) == sorted(Violation("deep-import", "apps.ui.page", f"apps.lib.{name}") for name in deep)


def test_a_consumer_loading_an_implementation_by_name_is_a_deep_import(tmp_path: Path) -> None:
    # Codex, PR #6182: outside a pure scope only TYPE_CHECKING blocks were read from source.
    consumer = 'import importlib\nimportlib.import_module("apps.lib.impl")\nimportlib.import_module(NAME)\n'
    consumer += 'importlib.import_module("apps.lib.contract")\n'
    consumer += (
        'importlib.import_module(".impl", "apps.lib")\nimportlib.import_module("..contract", package="apps.lib.x")\n'
    )
    consumer += 'importlib.import_module(".impl", __package__)\nimportlib.import_module("...impl", "apps.lib")\n'
    consumer += 'TARGET = "apps.lib.impl"\nimportlib.import_module(TARGET)\n'
    consumer += 'SHIFTY = "apps.lib.contract"\nSHIFTY = "apps.ui.other"\nimportlib.import_module(SHIFTY)\n'
    consumer += 'import runpy\nrunpy.run_path("apps/lib/impl.py")\nrunpy.run_path("./apps/lib/contract/__init__.py")\n'
    consumer += 'importlib.import_module(name="apps.lib.impl")\nrunpy.run_module(mod_name="apps.lib.impl")\n'
    consumer += 'runpy.run_path(path_name="apps/lib/impl.py")\nimportlib.import_module(package="apps.lib")\n'
    consumer += 'getattr(importlib, LOADER)("apps.lib.impl")\n(LOAD or OTHER)("apps.lib.unknown")\n'
    repo = _tree(tmp_path, {"apps/ui/__init__.py": "", "apps/ui/page.py": consumer})
    known = {"apps.ui", "apps.ui.page", "apps.lib.impl", "apps.lib.contract"}
    pairs = loaded_imports(repo, known)
    # A relative name with a literal anchor (lines 5 and 6) is read too, and so is a module
    # constant bound once to text (line 10) (Codex, PR #6182). Controls: a computed name (line 3)
    # or anchor (line 7) is purity's concern, a name with more dots than its anchor has parts
    # (line 8) fails at run time, and a contract is allowed. A name bound twice (line 13) may hold
    # either text, so each is read as loaded. Lines 15 and 16 run a module by its repository path, and 17 to 19 name
    # the target by keyword, while 20 names only an anchor; 21 hands a module's name to a computed
    # callee, while 22 hands it a name no module has (Codex, PR #6182).
    impl, contract = ("apps.ui.page", "apps.lib.impl"), ("apps.ui.page", "apps.lib.contract")
    assert pairs == {impl: [2, 5, 10, 15, 17, 18, 19, 21], contract: [4, 6, 13, 16]}
    assert _check(*pairs) == [Violation("deep-import", "apps.ui.page", "apps.lib.impl")]


def test_a_consumer_naming_no_loader_is_still_read_for_a_module_name(tmp_path: Path) -> None:
    # Codex, PR #6182: a file is read only when its text holds a loader's name, and
    # `getattr(LIB, NAME)("apps.lib.impl")` holds none. Control: a name no module has is no load.
    files = {
        "apps/ui/__init__.py": "",
        "apps/ui/page.py": 'print("apps.lib.none")\ngetattr(LIB, NAME)("apps.lib.impl")\n',
        "apps/ui/path.py": "getattr(LIB, NAME)('apps/lib/impl.py')\n",  # each needle on its own
        # Codex, PR #6182: a loader bound by destructuring is a name no binding rule follows.
        "apps/ui/unpacked.py": 'import importlib\n(load,) = (importlib.import_module,)\nload("apps.lib.impl")\n',
        # Codex, PR #6182: code run from text imports what it holds. Controls: text that is no
        # code, and code naming no module, import nothing.
        "apps/ui/ran.py": 'exec("pass")\nexec("import apps.lib.impl")\neval("(")\ncompile("from apps.lib import impl", "f", "exec")\n'
        # Codex, PR #6182: code handed by keyword runs too. Control: keyword code naming no module.
        'compile(source="import apps.lib.impl", filename="f", mode="exec")\ncompile(source="pass", filename="f", mode="exec")\n',
        # Codex, PR #6182: a spec factory names the module in its second argument.
        "apps/ui/spec.py": 'import importlib.util as u\ns = u.spec_from_file_location("alias", "apps/lib/impl.py")\n'
        'f(x, location="apps/lib/impl.py")\nRecord(guard_site="apps/lib/impl.py")\n',  # control: a record, no load
        # Codex, PR #6182: text built from literals with no side effect folds to the name it
        # always makes. Controls: ``!r`` adds quotes, and a call's result is not known.
        "apps/ui/folded.py": (
            'import importlib\nLEAF = "impl"\nimportlib.import_module("apps.lib." + "impl")\n'
            'importlib.import_module(f"apps.lib.{LEAF}")\nimportlib.import_module(".".join(("apps", "lib", LEAF)))\n'
            'importlib.import_module(f"apps.lib.{LEAF!r}")\nimportlib.import_module("apps.lib.impl" + suffix())\n'
            # Codex, PR #6182: an item picked from a written-out display. Controls: the wrong
            # item, and an index nobody can read.
            'importlib.import_module(("apps.lib.impl",)[0])\nimportlib.import_module({"k": "apps.lib.impl"}["k"])\n'
            'importlib.import_module(["x", "apps.lib.impl", "y"][-2])\nimportlib.import_module(("apps.lib.impl", "x")[1])\n'
            'importlib.import_module(("apps.lib.impl",)[i])\n'
            # A key after ``**other`` is certain; one before it may be replaced (control).
            'importlib.import_module({**o, "k": "apps.lib.impl"}["k"])\n'
            'importlib.import_module({"k": "apps.lib.impl", **o}["k"])\n'
        ),
        # Codex, PR #6182: an evaluator bound to a name, code handed to a callee named nothing
        # a reader knows, and a module constant folded from parts. Controls: text with no import, and
        # an import of no module from the repository.
        "apps/ui/aliased.py": (
            'run = exec\nrun("import apps.lib.impl")\nTARGET = "apps.lib." + "impl"\n'
            'importlib.import_module(TARGET)\nrun("x = 1")\nrun("import os")\n'
        ),
        "apps/ui/handed.py": 'r("import apps.lib.impl")\n',
        # Codex, PR #6182: a wrapper forwards a module name to its loader under any keyword.
        # Codex, PR #6182: arguments unpacked from a display, or from a name bound to one. Controls:
        # a path under a record keyword stays unread, and an unknown spread loads nothing known.
        "apps/ui/spread.py": (
            'import importlib\nimportlib.import_module(*("apps.lib.impl",))\n'
            'importlib.import_module(**{"name": "apps.lib.impl"})\nARGS = ("apps.lib.impl",)\n'
            'importlib.import_module(*ARGS)\nKW = {"name": "apps.lib.impl"}\nimportlib.import_module(**KW)\n'
            'Record(**{"guard_site": "apps/lib/impl.py"})\nimportlib.import_module(*rest)\n'
        ),
        # Codex, PR #6182: an item of a name bound to a display, read directly or through a name.
        # Controls: a display that picks itself ends, and an index past the end picks nothing.
        "apps/ui/named.py": (
            'import importlib\nTARGETS = ("apps.lib.impl",)\nimportlib.import_module(TARGETS[0])\n'
            'TABLE = {"k": "apps.lib.impl"}\nimportlib.import_module(TABLE["k"])\n'
            "SELF = (SELF[0],)\nimportlib.import_module(SELF[0])\nimportlib.import_module(TARGETS[1])\n"
            "B = TARGETS[0]\nimportlib.import_module(B)\n"
        ),
        # Codex, PR #6182: ``__import__`` imports each fromlist entry below the package it names,
        # by keyword, positionally, or through a bound name. Control: an entry naming no module.
        "apps/ui/fromlist.py": (
            '__import__("apps", fromlist=["lib.impl"])\n__import__("apps.lib", None, None, ("impl",))\n'
            'NAMES = ["impl"]\n__import__("apps.lib", fromlist=NAMES)\n__import__("apps.lib", fromlist=["none"])\n'
            # Codex, PR #6182: a package named through a name bound in a function scope.
            'def f():\n    base = "apps"\n    __import__(base, fromlist=["lib.impl"])\n'
            # Codex, PR #6182: a loader bound by destructuring, and one reached by reflection.
            '(load,) = (__import__,)\nload("apps", fromlist=["lib.impl"])\n'
            'getattr(builtins, NAME)("apps.lib", None, None, ["impl"])\n'
        ),
        "apps/ui/wrapped.py": 'def load(**kw):\n    return importlib.import_module(kw["module"])\nload(module="apps.lib.impl")\n',
        # Codex, PR #6182: a name bound in a function scope, to a module's name or to code.
        "apps/ui/local.py": (
            'def f():\n    T = "apps.lib.impl"\n    importlib.import_module(T)\n'
            'def g():\n    C = "import apps.lib.impl"\n    run(C)\n    T = "apps.lib.none"\n'
            # Codex, PR #6182: a name bound to a name holds its texts, whichever binding is read
            # first. Control: a name bound to an unknown name holds none.
            'def h():\n    B = A\n    if ready:\n        A = "apps.lib.impl"\n    importlib.import_module(B)\n'
            "    D = other\n    importlib.import_module(D)\n"
        ),
        # Codex, PR #6182: a bound name inside a composed text, by f-string, concatenation or
        # join, through each display a name joined is bound to, and grown in a loop. Controls: a
        # ``!r`` part, a part nobody can read, a separator that makes no module's name, and a spread.
        "apps/ui/composed.py": (
            'def f():\n    leaf = "impl"\n    importlib.import_module(f"apps.lib.{leaf}")\n'
            '    importlib.import_module("apps.lib." + leaf)\n    importlib.import_module(".".join(("apps", "lib", leaf)))\n'
            '    parts = ["apps", "lib", leaf]\n    importlib.import_module(".".join(parts))\n'
            '    importlib.import_module(f"apps.lib.{leaf!r}")\n    importlib.import_module(f"apps.lib.{other}")\n'
            '    importlib.import_module("/".join(parts))\n'
            '    name = "apps"\n    for part in ("lib", "impl"):\n        name = name + "." + part\n'
            "    importlib.import_module(name)\n"
            '    BOTH = ["x"]\n    BOTH = ["apps", "lib", "impl"]\n    importlib.import_module(".".join(BOTH))\n'
            '    importlib.import_module(".".join(("apps", "lib", *rest)))\n'
        ),
        # Codex, PR #6182: a parameter bound by its default, positional, positional-only,
        # keyword-only or a lambda's. Control: a parameter with no default holds no text.
        "apps/ui/defaults.py": (
            'def a(target="apps.lib.impl"):\n    importlib.import_module(target)\n'
            'def b(x, /, y="apps.lib.impl"):\n    importlib.import_module(y)\n'
            'def c(*, k="apps.lib.impl", j):\n    importlib.import_module(k)\n'
            'd = lambda t="apps.lib.impl": importlib.import_module(t)\n'
            'def e(p, q="x"):\n    importlib.import_module(p)\n    importlib.import_module(j)\n'
        ),
        # Codex, PR #6182: a template filled by ``%`` or ``str.format``, by literal or bound name,
        # one value or a tuple, by position, index or keyword, or a composed template. Controls:
        # a field that formats (``%r``, ``{!r}``), reaches into its value (``{0.x}``) or names
        # nothing given, and a value left over, fill nothing.
        "apps/ui/formatted.py": (
            'importlib.import_module("apps.lib.%s" % "impl")\n'
            'def f():\n    leaf = "impl"\n    importlib.import_module("apps.%s.%s" % ("lib", leaf))\n'
            '    importlib.import_module("apps.{}.{}".format("lib", leaf))\n'
            '    importlib.import_module("{1}.{0}.impl".format("lib", "apps"))\n'
            '    importlib.import_module("apps.lib.{leaf}".format(leaf=leaf))\n'
            '    importlib.import_module("apps.lib.%r" % leaf)\n    importlib.import_module("apps.lib.{!r}".format(leaf))\n'
            '    importlib.import_module("apps.lib.{0.x}".format(leaf))\n'
            '    importlib.import_module("apps.lib.{2}".format(leaf))\n'
            '    importlib.import_module("apps.lib.%s" % ("impl", "x"))\n'
            '    importlib.import_module(("apps.lib.%s" + "") % leaf)\n'
        ),
        # Codex, PR #6182: an expression that evaluates to one of its parts, either branch of a
        # conditional, any operand of ``or``/``and``, a walrus's value, nested in a composed text
        # too. Control: a conditional whose branches name no module.
        "apps/ui/branches.py": (
            'importlib.import_module("apps.lib.contract" if on else "apps.lib.impl")\n'
            'importlib.import_module(name or "apps.lib.impl")\nimportlib.import_module(ok and "apps.lib.impl")\n'
            'importlib.import_module((t := "apps.lib.impl"))\n'
            'importlib.import_module("apps.lib." + ("impl" if on else "x"))\n'
            'importlib.import_module("x" if on else "y")\n'
        ),
        # Codex, PR #6182: a name bound by destructuring, written out, nested, through a name bound
        # to a display, as a loop's target, and beside a starred target. Controls: the starred
        # target itself (a list), a length mismatch, and a spread value that shifts every position.
        "apps/ui/unpacked_text.py": (
            'def f():\n    (target,) = ("apps.lib.impl",)\n    importlib.import_module(target)\n'
            '    a, (b, c) = "x", ("y", "apps.lib.impl")\n    importlib.import_module(c)\n'
            '    PAIR = ("x", "apps.lib.impl")\n    _, d = PAIR\n    importlib.import_module(d)\n'
            '    for _, e in (("x", "apps.lib.impl"),):\n        importlib.import_module(e)\n'
            '    *g, h = ("apps.lib.impl", "x")\n    importlib.import_module(g)\n'
            '    k, *rest = ("apps.lib.impl", "x")\n    importlib.import_module(k)\n'
            '    i, j = ("apps.lib.impl",)\n    importlib.import_module(i)\n'
            '    m, n = (*rest, "apps.lib.impl")\n    importlib.import_module(n)\n'
        ),
        # Codex, PR #6182: a display reached through a name bound to another name, read before or
        # after it, or through a conditional's branch. Control: an alias of a display picking nothing.
        "apps/ui/aliased_display.py": (
            'TARGETS = ("apps.lib.impl",)\nALIASED = TARGETS\nimportlib.import_module(ALIASED[0])\n'
            'def f():\n    LATE = EARLY\n    EARLY = ["apps.lib.impl"]\n    importlib.import_module(LATE[0])\n'
            '    EITHER = TARGETS if on else ("x",)\n    importlib.import_module(EITHER[0])\n'
            "    importlib.import_module(ALIASED[1])\n"
        ),
        # Codex, PR #6182: a text a text method or a slice computes, by position or keyword,
        # chained, and on a name bound to a text. Controls: a method that leaves no module's name,
        # one that raises, a slice cutting a name short, and a method that makes no text.
        "apps/ui/methods.py": (
            'importlib.import_module("apps.lib.contract".replace("contract", "impl"))\n'
            'importlib.import_module("  apps.lib.impl ".strip())\nimportlib.import_module("APPS.LIB.IMPL".lower())\n'
            'importlib.import_module("_apps.lib.impl"[1:])\nT = "apps.lib.impl_"\nimportlib.import_module(T[:-1])\n'
            'importlib.import_module("xapps.lib.impl".removeprefix("x").replace("q", "z"))\n'
            'importlib.import_module("APPS.LIB.IMPL".lower().expandtabs(tabsize=4))\n'
            'importlib.import_module("apps.lib.contract".replace("contract", "none"))\n'
            'importlib.import_module("apps.lib.impl".replace(1, 2))\nimportlib.import_module(T[:-2])\n'
            'importlib.import_module("apps.lib.impl".split("."))\n'
            'importlib.import_module("apps.lib.imp" + "xl"[1])\nimportlib.import_module("apps.lib.imp" + "xl"[0])\n'
        ),
        # Sol, PR #6182: a file whose text names no loader and no whole module was never read,
        # though its parts fold to one. Control: parts folding to no module's name.
        "apps/ui/split.py": 'registry.get("apps" + ".lib.impl")\nregistry.get("apps" + ".lib.none")\n',
        # Codex, PR #6182: a wrapper's parameter takes the arguments its calls write out, by
        # position, keyword, through a method and a lambda, and after a spread onto any later
        # parameter. Controls: an argument naming no module, a call of a function this module does
        # not define, and an argument before a spread, which keeps its own position. The load
        # reads at the loader, never at a call handing a part.
        "apps/ui/wrapper.py": (
            'def load(leaf, *, pkg="lib"):\n    importlib.import_module("apps." + pkg + "." + leaf)\n'
            'load("impl")\nload(leaf="impl")\nload("x", pkg="none")\n'
            'class C:\n    def get(self, part):\n        importlib.import_module(f"apps.lib.{part}")\n'
            'C().get("impl")\nfetch = lambda tail: importlib.import_module("apps.lib." + tail)\nfetch("impl")\n'
            'def other(item):\n    importlib.import_module("apps.lib." + item)\nunknown("impl")\n'
            'def kw(*, only):\n    importlib.import_module("apps.lib." + only)\nkw(only="impl")\n'
            'def two(a, b):\n    importlib.import_module("apps.lib." + b)\ntwo(*rest, "impl")\n'
            'def three(c, d):\n    importlib.import_module("apps.lib." + d)\nthree("impl", *rest)\n'
        ),
        # Codex, PR #6182: a name stored on an attribute, of an instance or a class, set in one
        # place and handed to a call in another. Control: an attribute set to no module's name.
        "apps/ui/attribute.py": (
            'class C:\n    kind = "apps.lib.impl"\n    def __init__(self):\n        self.target = "apps.lib.impl"\n'
            "    def run(self):\n        registry.get(self.target)\n        registry.get(C.kind)\n"
            '        self.other = "apps.lib.none"\n        registry.get(self.other)\n'
        ),
        # The price: any call handed the name reads as a load; the same literal outside a call does not.
        "apps/ui/kept.py": 'SELF = {"apps.lib.impl": 1}\nitems = [SELF.get("apps.lib.impl")]\n',
    }
    known = {
        "apps.ui",
        "apps.ui.page",
        "apps.ui.path",
        "apps.ui.unpacked",
        "apps.ui.kept",
        "apps.ui.ran",
        "apps.ui.spec",
        "apps.ui.folded",
        "apps.ui.aliased",
        "apps.ui.handed",
        "apps.ui.wrapped",
        "apps.ui.fromlist",
        "apps.ui.named",
        "apps.ui.spread",
        "apps.ui.local",
        "apps.ui.composed",
        "apps.ui.defaults",
        "apps.ui.formatted",
        "apps.ui.branches",
        "apps.ui.unpacked_text",
        "apps.ui.aliased_display",
        "apps.ui.methods",
        "apps.ui.split",
        "apps.ui.wrapper",
        "apps.ui.attribute",
        "apps.lib.impl",
    }
    assert loaded_imports(_tree(tmp_path, files), known) == {
        ("apps.ui.page", "apps.lib.impl"): [2],
        ("apps.ui.path", "apps.lib.impl"): [1],
        ("apps.ui.unpacked", "apps.lib.impl"): [3],
        ("apps.ui.kept", "apps.lib.impl"): [2],
        ("apps.ui.ran", "apps.lib.impl"): [2, 4, 5],
        ("apps.ui.spec", "apps.lib.impl"): [2, 3],
        ("apps.ui.folded", "apps.lib.impl"): [3, 4, 5, 8, 9, 10, 13],
        ("apps.ui.folded", "x"): [11],  # the item picked, not the display it sits in
        ("apps.ui.aliased", "apps.lib.impl"): [2, 4],
        ("apps.ui.handed", "apps.lib.impl"): [1],
        ("apps.ui.wrapped", "apps.lib.impl"): [3],
        ("apps.ui.fromlist", "apps.lib.impl"): [1, 2, 4, 8, 10, 11],
        ("apps.ui.fromlist", "apps"): [1, 10],  # the package each call names is loaded too
        ("apps.ui.fromlist", "apps.lib"): [2, 4, 5],
        ("apps.ui.named", "apps.lib.impl"): [3, 5, 10],
        ("apps.ui.spread", "apps.lib.impl"): [2, 3, 5, 7],
        ("apps.ui.local", "apps.lib.impl"): [3, 6, 12],
        ("apps.ui.composed", "apps.lib.impl"): [3, 4, 5, 7, 14, 17],
        ("apps.ui.defaults", "apps.lib.impl"): [2, 4, 6, 7],
        ("apps.ui.formatted", "apps.lib.impl"): [1, 4, 5, 6, 7, 13],
        ("apps.ui.branches", "apps.lib.impl"): [1, 2, 3, 4, 5],
        ("apps.ui.unpacked_text", "apps.lib.impl"): [3, 5, 8, 10, 14],
        ("apps.ui.aliased_display", "apps.lib.impl"): [3, 7, 9],
        ("apps.ui.methods", "apps.lib.impl"): [1, 2, 3, 4, 6, 7, 8, 13],
        ("apps.ui.split", "apps.lib.impl"): [1],
        ("apps.ui.wrapper", "apps.lib.impl"): [2, 8, 10, 16, 19],
        ("apps.ui.attribute", "apps.lib.impl"): [6, 7],
    }


def test_enforced_roots_and_contracts_on_this_tree_are_pure_from_source() -> None:
    pairs = pure_scope_imports(REPO, sorted(_enforced()), set())
    assert pairs, "read no imports at all: the scan is pointed at nothing"
    assert _check(*pairs, enforced=tuple(sorted(_enforced()))) == []
