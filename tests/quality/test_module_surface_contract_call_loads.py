"""Split from tests/quality/test_module_surface_contract.py (#10941; behaviour unchanged).

call_loads folding from entry points through pickle readers.
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


def test_pydoc_locate_loads_the_module_its_object_path_names(tmp_path: Path) -> None:
    # Codex, PR #6182: ``pydoc.locate("apps.other.impl.run")`` imports apps.other.impl to find
    # ``run``, but no reader knew pydoc's loader. A dotted object path given to a loader now loads its
    # longest module prefix (pkgutil's resolver walks it the same way without a colon). Controls:
    # ``locate`` is no loader where pydoc cannot be held (``apps.reconcile.locate`` is a module, and a
    # CLI command may be named "locate"), a path no module prefixes stays as written, and a module
    # that only compares a text to "pydoc" holds no pydoc.
    files = {
        "apps/__init__.py": "",
        "apps/other/__init__.py": "",
        "apps/other/impl.py": "",
        "scripts/__init__.py": "",
        "scripts/located.py": 'import pydoc\npydoc.locate("apps.other.impl.run")\n',
        "scripts/walked.py": 'import pkgutil\npkgutil.resolve_name("apps.other.impl.run")\n',
        "scripts/aliased.py": "from pydoc import locate as find\nfind(name)\n",
        "scripts/bare.py": "from pydoc import locate\nlocate(name)\n",
        "scripts/reflected.py": 'import pydoc\nf = getattr(pydoc, "locate")\n',
        "scripts/texted.py": 'm = __import__("pydoc")\nm.locate(name)\n',
        "scripts/outside.py": 'import pydoc\npydoc.locate("json.dumps")\n',
        "scripts/other.py": 'from apps.other import impl as locate\nlocate.run()\ncli("locate", locate)\n',
        # Codex, PR #6182: every pydoc entry point resolves a dotted text by importing it
        "scripts/rendered.py": 'import pydoc as p\np.render_doc("apps.other.impl.run")\np.resolve(thing)\n',
        "scripts/member.py": 'from pydoc import render_doc as show\nshow("apps.other.impl.run")\n',
        "scripts/named.py": 'm = __import__("pydoc")\nm.render_doc("apps.other.impl.run")\n',
        "scripts/tested.py": 'def f(name):\n    return name == "pydoc" and render_doc(name) and log.info(name)\n',
    }
    known = {"apps", "apps.other", "apps.other.impl", "scripts"} | {
        f"scripts.{Path(f).stem}" for f in files if f.startswith("scripts/") and "__init__" not in f
    }
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] in {DYNAMIC, "apps.other.impl"}} == {
        ("scripts.located", "apps.other.impl"): [2],
        ("scripts.walked", "apps.other.impl"): [2],
        ("scripts.aliased", DYNAMIC): [2],
        ("scripts.bare", DYNAMIC): [2],
        ("scripts.reflected", DYNAMIC): [2],
        ("scripts.texted", DYNAMIC): [2],
        ("scripts.rendered", "apps.other.impl"): [2],
        ("scripts.rendered", DYNAMIC): [3],
        ("scripts.member", "apps.other.impl"): [2],
        ("scripts.named", "apps.other.impl"): [2],
    }
    assert loads[("scripts.outside", "json.dumps")] == [2]


def test_a_loader_taken_from_a_scope_namespace_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``globals()["import" + "_module"](target)`` calls an imported loader that no
    # name in the source reads. A module that imports from a loader module and reads a whole
    # scope's namespace holds the loader loosely. Controls: the same read in a module that imports
    # no loader module, and an imported loader module read only by attribute, are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/scoped.py": 'from importlib import import_module\nglobals()["import" + "_module"](target)\n',
        "scripts/local.py": "import importlib\ndef f():\n    return locals()[k](target)\n",
        "scripts/framed.py": "import runpy\nimport sys\nsys._getframe().f_globals[k](target)\n",
        "scripts/plain.py": "import json\nglobals()[k](row)\n",
        "scripts/named.py": "import importlib\nimportlib.util.decode_source(data)\nHANDLERS[k](row)\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("scripts.scoped", DYNAMIC): [2],
        ("scripts.local", DYNAMIC): [3],
        ("scripts.framed", DYNAMIC): [3],
    }


def test_an_entry_point_load_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``EntryPoint(name, value, group).load()`` imports the ``module:attr`` text
    # it carries, runtime data no reader can see. Where importlib.metadata (or its backport) can be
    # held, a bare ``.load()`` reads as DYNAMIC. Controls: a ``.load()`` where no entry point can be
    # held, a load handed an argument, a load read straight off an imported module, and a text
    # only compared to "importlib.metadata" are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/built.py": (
            'from importlib.metadata import EntryPoint\nEntryPoint(name="x", value=value, group="g").load()\n'
        ),
        "scripts/listed.py": (
            'import importlib.metadata\nfor ep in importlib.metadata.entry_points(group="g"):\n    ep.load()\n'
        ),
        "scripts/backport.py": "import importlib_metadata\nplugin.load()\n",
        "scripts/texted.py": 'md = __import__("importlib.metadata")\nplugin.load()\n',
        "scripts/store.py": "STORE.load()\n",
        "scripts/argued.py": "import importlib.metadata\nimport json\njson.load(fh)\nstore.load(path)\n",
        "scripts/offmod.py": "import importlib.metadata\nimport cache\ncache.load()\n",
        "scripts/compared.py": 'def f(name):\n    return name == "importlib.metadata" and store.load()\n',
        # Codex, PR #6182: the loader handed on before it is called
        "scripts/handed.py": "import importlib.metadata\nload = ep.load\nload()\n",
        "scripts/offhand.py": "import importlib.metadata\nimport json\nread = json.load\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("scripts.built", DYNAMIC): [2],
        ("scripts.listed", DYNAMIC): [3],
        ("scripts.backport", DYNAMIC): [2],
        ("scripts.texted", DYNAMIC): [2],
        ("scripts.handed", DYNAMIC): [2],
    }


def test_an_evaluator_named_as_text_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``run = getattr(builtins, "exec")`` then ``run(code)`` runs code no reader
    # sees. Where builtins can be held, an evaluator's name as text reads as DYNAMIC, as a
    # loader's does. Controls: the same text where builtins cannot be held (a ``compile`` mode),
    # and one only compared, are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/reflected.py": 'import builtins\nrun = getattr(builtins, "exec")\nrun(code)\n',
        "scripts/spaced.py": 'b = __builtins__\nrun = b.get("eval")\n',
        "scripts/mode.py": 'MODE = "exec"\ndef f(tree):\n    return parse(tree, MODE)\n',
        "scripts/compared.py": 'import builtins\ndef f(name):\n    return name == "exec"\n',
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("scripts.reflected", DYNAMIC): [2],
        ("scripts.spaced", DYNAMIC): [1, 2],
    }


def test_import_machinery_calls_are_loaders(tmp_path: Path) -> None:
    # Codex, PR #6182: ``files(target)`` from importlib.resources imports ``target``, as does
    # ``find_spec`` for a dotted name's parents. Every call into the import machinery reads as a
    # load: text names its module, and any argument that is not text is DYNAMIC. Controls: calls the machinery makes
    # without importing (a distribution's version, decoding source bytes, listing a path's
    # modules) are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/other.py": "",
        "scripts/resourced.py": "from importlib.resources import files\nfiles(target)\n",
        "scripts/named.py": 'import importlib.resources\nimportlib.resources.files("scripts.other")\n',
        "scripts/probed.py": "import importlib.util\nimportlib.util.find_spec(name)\n",
        "scripts/anchored.py": 'import importlib.util\nimportlib.util.find_spec("scripts.other", package)\n',
        "scripts/versioned.py": "import importlib.metadata\nimportlib.metadata.version(name)\n",
        "scripts/decoded.py": "import importlib.util\nimportlib.util.decode_source(data)\n",
        "scripts/listed.py": "import pkgutil\npkgutil.iter_modules(path)\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[0] != pair[1]} == {
        ("scripts.resourced", DYNAMIC): [2],
        ("scripts.named", "scripts.other"): [2],
        ("scripts.probed", DYNAMIC): [2],
        ("scripts.anchored", "scripts.other"): [2],
        ("scripts.anchored", DYNAMIC): [2],
    }


def test_a_module_table_lookup_is_a_load(tmp_path: Path) -> None:
    # Codex, PR #6182: ``sys.modules["pkg.mod"]`` hands out a module another module imported,
    # with no import statement or loader call, and so does the table of a ``sys`` a loader
    # returns (``__import__("sys").modules``); ``sys`` handed on is DYNAMIC. A lookup by text is an edge to that module, a
    # lookup by any other key or any other read of the table is DYNAMIC. Controls: a membership
    # test, the module's own entry, writing or deleting an entry, a key no module answers, and
    # one named member of ``sys`` are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/other.py": "",
        "scripts/keyed.py": 'import sys\nsys.modules["scripts.other"].run()\n',
        "scripts/fetched.py": 'import sys\nsys.modules.get("scripts.other")\nsys.modules.get(name)\n',
        "scripts/imported.py": 'from sys import modules\nmodules["scripts.other"]\n',
        "scripts/handed.py": 'import sys\nlookup("scripts.other", sys.modules.get)\n',
        "scripts/loaded.py": '__import__("sys").modules["scripts.other"]\n',
        "scripts/imported_sys.py": 'import importlib\nimportlib.import_module("sys").modules.get(name)\n',
        "scripts/reflected.py": 'import sys\ngetattr(sys, "modules")["scripts.other"]\n',
        "scripts/aliased.py": "import sys\ns = sys\ns.modules\n",
        "scripts/dunder.py": 'import sys\nsys.__dict__["modules"]\n',
        "scripts/argv.py": 'import sys\n__import__("sys").argv\nsys.argv\n',
        "scripts/walked.py": "import sys\nfor m in sys.modules.values():\n    m.run()\n",
        "scripts/member.py": 'import sys\nif "scripts.other" in sys.modules:\n    pass\n',
        "scripts/own.py": "import sys\nsys.modules[__name__]\n",
        "scripts/written.py": "import sys\nsys.modules[k] = v\ndel sys.modules[k]\n",
        "scripts/popped.py": 'import sys\nsys.modules.pop("rbox", None)\n',
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    expected = {
        ("scripts.keyed", "scripts.other"): [2],
        ("scripts.fetched", "scripts.other"): [2],
        ("scripts.fetched", DYNAMIC): [3],
        ("scripts.imported", "scripts.other"): [2],
        ("scripts.handed", "scripts.other"): [2],  # text naming a module is read wherever it is
        ("scripts.handed", DYNAMIC): [2],
        ("scripts.walked", DYNAMIC): [2],
        ("scripts.loaded", "scripts.other"): [1],
        ("scripts.imported_sys", DYNAMIC): [2],
        ("scripts.reflected", DYNAMIC): [2],
        ("scripts.aliased", DYNAMIC): [2],
        ("scripts.dunder", DYNAMIC): [2],
    }
    # A loader handed ``sys`` loads ``sys`` too; that edge leaves the tree.
    assert {pair: lines for pair, lines in loads.items() if pair[0] != pair[1] and pair[1] != "sys"} == expected
    keyed = files["scripts/keyed.py"]
    assert sorted(source_imports(keyed, "scripts.keyed", False, known)) == [("scripts.other", 2), ("sys", 1)]


def test_a_pickle_reader_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: unpickling imports every module the stream names, so ``pickle.loads(payload)``
    # can load ``apps.x.impl``. Every call into a pickle reader, or one handed on, is DYNAMIC,
    # whatever it is handed, since even literal bytes name modules no reader parses. A part of the
    # machinery imported in two functions is held as one import would hold it. Controls: writing
    # a pickle, and a name ``pickle`` no import binds, are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/loaded.py": "import pickle\npickle.loads(payload)\n",
        "scripts/literal.py": 'import pickle\npickle.loads(b"cscripts.other\\nrun\\n.")\n',
        "scripts/reader.py": "from pickle import Unpickler\nUnpickler(f).load()\n",
        "scripts/shelved.py": 'import shelve\nshelve.open("db")\n',
        "scripts/model.py": "def a():\n    import joblib\n    joblib.dump(m, p)\ndef b():\n    import joblib\n    joblib.load(p)\n",
        "scripts/probed.py": "import importlib.util\nimport importlib.metadata\nimportlib.util.find_spec(name)\n",
        "scripts/handed.py": "import pickle\nread = pickle.load\n",
        "scripts/written.py": "import pickle\npickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)\n",
        "scripts/local.py": "pickle = make()\npickle.loads(payload)\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[0] != pair[1]} == {
        ("scripts.loaded", DYNAMIC): [2],
        ("scripts.literal", DYNAMIC): [2],
        ("scripts.reader", DYNAMIC): [2],
        ("scripts.shelved", DYNAMIC): [2],
        ("scripts.model", DYNAMIC): [6],
        ("scripts.probed", DYNAMIC): [3],
        ("scripts.handed", DYNAMIC): [2],
    }


def test_the_interactive_interpreter_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``code.InteractiveInterpreter().runsource(text)`` runs text as code, as
    # ``exec`` does, through an interpreter object no reader follows. Every call into ``code`` or
    # ``codeop``, or either handed on, reads as DYNAMIC. Control: a name ``code`` no import binds.
    files = {
        "scripts/__init__.py": "",
        "scripts/sourced.py": "import code\ncode.InteractiveInterpreter().runsource(src)\n",
        "scripts/console.py": "from code import InteractiveConsole\nInteractiveConsole().push(src)\n",
        "scripts/compiled.py": "import codeop\ncodeop.compile_command(src)\n",
        "scripts/handed.py": "import code\nrun = code.interact\n",
        "scripts/local.py": "code = make()\ncode.run(src)\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("scripts.sourced", DYNAMIC): [2],
        ("scripts.console", DYNAMIC): [2],
        ("scripts.compiled", DYNAMIC): [2],
        ("scripts.handed", DYNAMIC): [2],
    }
