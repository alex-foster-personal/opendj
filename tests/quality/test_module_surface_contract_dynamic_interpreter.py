"""Split from tests/quality/test_module_surface_contract.py (#10941; behaviour unchanged).

Interpreter argv folding and import-machinery hand-offs.
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


def test_a_module_named_inside_a_display_is_loaded(tmp_path: Path) -> None:
    # Codex, PR #6182: ``run([sys.executable, "-m", "apps.x.impl"])`` names its module inside a
    # list, inline or bound to a name. Every item a display holds, at any depth, is read as an
    # argument is. Controls: texts that name no module, and a path in a display handed to a
    # keyword no loader takes a path by, are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/other.py": "",
        "scripts/argv.py": 'import sys\nrun([sys.executable, "-m", "scripts.other"])\n',
        "scripts/bound.py": 'import sys\nCMD = (sys.executable, "-m", "scripts.other")\nrun(CMD)\n',
        "scripts/nested.py": 'run(env={"M": ["scripts.other"]})\n',
        "scripts/spread.py": 'TAIL = ["scripts.other"]\nrun(["-m", *TAIL])\n',
        "scripts/summed_names.py": 'TAIL = ["scripts.other"]\nrun(base + TAIL)\n',
        "scripts/pathed.py": 'import sys\nrun([sys.executable, "scripts/other.py"])\n',
        "scripts/plain.py": 'run(["ls", "-l"])\n',
        "scripts/listed.py": 'report(files=["scripts/other.py"])\n',
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == "scripts.other"} == {
        ("scripts.argv", "scripts.other"): [2],
        ("scripts.bound", "scripts.other"): [3],
        ("scripts.nested", "scripts.other"): [1],
        ("scripts.spread", "scripts.other"): [2],
        ("scripts.summed_names", "scripts.other"): [2],
        ("scripts.pathed", "scripts.other"): [2],
    }


def test_an_interpreter_run_of_an_unread_operand_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``run([sys.executable, "-m", target])`` runs whatever ``target`` holds. An
    # ``-m`` or ``-c`` whose operand no reader folds, or with nothing after it in its display, is
    # DYNAMIC, in a display or as the call's own arguments, where an item before it names an
    # interpreter. ``"-mmod"`` and a command line name their module, and a command line built at
    # run time (an f-string, ``+``, ``%`` or ``.format``) with an unread operand is DYNAMIC. An argv
    # grown by ``+=``, ``extend``, ``append`` or ``insert`` is read whole. Controls: a folded operand is
    # its edge alone; ``-m`` as a word inside other text, another option with an unread operand,
    # ``git log -m``, an option with no program before it, a record's prose, and the DYNAMIC
    # marker's own text are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/other.py": "",
        "scripts/target.py": 'import sys\nrun([sys.executable, "-m", target])\n',
        "scripts/code.py": 'import sys\nrun((sys.executable, "-c", code))\n',
        "scripts/spawned.py": 'import os\nos.spawnl(mode, python, python, "-m", target)\n',
        "scripts/trailing.py": 'import sys\nrun([sys.executable, "-m"] + rest)\n',
        "scripts/keyword.py": 'run(args=["/usr/bin/python3", "-m", target])\n',
        "scripts/git.py": 'run([git, "log", "-m", span])\n',
        "scripts/flagged.py": 'word.startswith("-m")\n',
        "scripts/record.py": 'Surface(entrypoint="python -m scripts.other --live")\n',
        "scripts/cmdkw.py": 'run(cmd="python -m scripts.other")\n',
        "scripts/marker.py": 'note("<dynamic import>")\n',
        "scripts/fstring.py": 'import os\nos.system(f"python -m {target}")\n',
        "scripts/added.py": 'run("python3 -c " + code, shell=True)\n',
        "scripts/percent.py": 'run(cmd="%s -m %s" % (exe, target))\n',
        "scripts/formatted.py": 'run("python -m{}".format(target))\n',
        "scripts/folded_f.py": 'BASE = "scripts"\nos.system(f"python -m {BASE}.other")\n',
        "scripts/git_f.py": 'run(f"{git} log -m {span}")\n',
        "scripts/prose_f.py": 'Surface(entrypoint=f"python -m {target}")\n',
        "scripts/shell_c.py": "import os\nos.system(\"python -c 'import scripts.other'\")\n",
        "scripts/shell_true.py": "run('python3 -c \"from scripts import other\"', shell=True)\n",
        "scripts/quoted_c.py": "os.system(\"python '-cimport scripts.other'\")\n",
        "scripts/grown.py": 'import sys\ncmd = [sys.executable]\ncmd += ["-m", target]\nrun(cmd)\n',
        "scripts/extended.py": 'import sys\ncmd = [sys.executable]\ncmd.extend(["-c", code])\nrun(cmd)\n',
        "scripts/appended.py": 'cmd = ["python3"]\ncmd.append("-m")\ncmd.append(target)\nrun(cmd)\n',
        "scripts/inserted.py": 'cmd = ["-m", "scripts.other"]\ncmd.insert(0, "python3")\nrun(cmd)\n',
        "scripts/front.py": 'import sys\ncmd = ["-m", target]\ncmd.insert(0, sys.executable)\nrun(cmd)\n',
        "scripts/placed.py": 'import sys\ncmd = [sys.executable]\ncmd.insert(1, "-m")\ncmd.insert(2, target)\nrun(cmd)\n',
        "scripts/placed_back.py": 'import sys\ncmd = [sys.executable, "x", target]\ncmd.insert(-1, "-m")\nrun(cmd)\n',
        "scripts/placed_anywhere.py": 'import sys\ncmd = [sys.executable]\ncmd.insert(i, "-m")\ncmd.insert(j, target)\nrun(cmd)\n',
        "scripts/placed_front.py": 'import sys\ncmd = ["-m", target]\ncmd.insert(i, sys.executable)\nrun(cmd)\n',
        "scripts/placed_first.py": 'import sys\ncmd = [target, sys.executable]\ncmd.insert(0, "-m")\nrun(cmd)\n',
        "scripts/quoted_f.py": 'import sys\nrun(f\'"{sys.executable}" "-m" "{target}"\', shell=True)\n',
        "scripts/quoted_m.py": 'os.system(\'python "-m" "scripts.other"\')\n',
        "scripts/shadowed.py": 'import sys\ntarget = "json"\ndef launch(target):\n    run([sys.executable, "-m", target])\n',
        "scripts/constant.py": 'import sys\nMOD = "scripts.other"\nrun([sys.executable, "-m", MOD])\n',
        "scripts/summed.py": 'import sys\ncmd = [sys.executable] + ["-m", target]\nrun(cmd)\n',
        "scripts/based.py": 'import sys\nPY = [sys.executable, "-m"]\nrun([*PY, "scripts.other"])\nrun(PY + ["scripts.other"])\n',
        "scripts/spread_runtime.py": 'import sys\nB = ["-m", target]\nrun([sys.executable, *B])\n',
        "scripts/grown_git.py": 'cmd = ["git", "log"]\ncmd += ["-m", span]\nrun(cmd)\n',
        "scripts/joined_c.py": "run(['python3', '-cimport scripts.other'])\n",
        "scripts/unclosed.py": 'os.system("python -c \'import scripts.other")\n',
        "scripts/joined.py": 'run([exe, "-mscripts.other"])\n',
        "scripts/shell.py": 'os.system("python -m scripts.other --fast")\n',
        "scripts/folded.py": 'run([exe, "-m", "scripts.other"])\n',
        "scripts/coded.py": 'import sys\nrun([sys.executable, "-c", "import scripts.other"])\n',
        "scripts/held_code.py": 'CMD = ("python3", "-c", "from scripts import other")\nrun(CMD)\n',
        "scripts/worded.py": 'log("rerun with -m and a module")\n',
        "scripts/other_option.py": 'run([exe, "-W", level])\n',
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[0] != pair[1]} == {
        ("scripts.target", DYNAMIC): [2],
        ("scripts.code", DYNAMIC): [2],
        ("scripts.spawned", DYNAMIC): [2],
        ("scripts.trailing", DYNAMIC): [2],
        ("scripts.keyword", DYNAMIC): [1],
        ("scripts.cmdkw", "scripts.other"): [1],
        ("scripts.fstring", DYNAMIC): [2],
        ("scripts.added", DYNAMIC): [1],
        ("scripts.percent", DYNAMIC): [1],
        ("scripts.formatted", DYNAMIC): [1],
        ("scripts.folded_f", "scripts.other"): [2],
        ("scripts.shell_c", "scripts.other"): [2],
        ("scripts.shell_true", "scripts.other"): [1],
        ("scripts.joined_c", "scripts.other"): [1],
        ("scripts.grown", DYNAMIC): [4],
        ("scripts.extended", DYNAMIC): [4],
        ("scripts.appended", DYNAMIC): [4],
        ("scripts.inserted", "scripts.other"): [3],
        ("scripts.summed", DYNAMIC): [3],
        ("scripts.front", DYNAMIC): [4],
        ("scripts.shadowed", DYNAMIC): [4],
        ("scripts.constant", "scripts.other"): [3],
        ("scripts.placed", DYNAMIC): [5],
        ("scripts.placed_back", DYNAMIC): [4],
        ("scripts.placed_anywhere", DYNAMIC): [5],
        ("scripts.placed_front", DYNAMIC): [4],
        ("scripts.quoted_f", DYNAMIC): [2],
        ("scripts.quoted_m", "scripts.other"): [1],
        ("scripts.based", "scripts.other"): [3, 4],
        ("scripts.spread_runtime", DYNAMIC): [3],
        ("scripts.quoted_c", "scripts.other"): [1],
        ("scripts.joined", "scripts.other"): [1],
        ("scripts.shell", "scripts.other"): [1],
        ("scripts.folded", "scripts.other"): [1],
        ("scripts.coded", "scripts.other"): [2],
        ("scripts.held_code", "scripts.other"): [2],
    }


def test_import_machinery_handed_on_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``load = files`` then ``load(target)`` calls the machinery through a name no
    # import binds. A loading part of the machinery read anywhere but as a callee reads as DYNAMIC
    # there. Controls: a non-loading part handed on, a loading part called, and a name the
    # machinery binds twice (no longer known to hold it) are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/aliased.py": "from importlib.resources import files\nload = files\nload(target)\n",
        "scripts/unpacked.py": "import importlib.util\n(probe,) = (importlib.util.find_spec,)\n",
        "scripts/passed.py": "import importlib\nrun(importlib.resources.files, name)\n",
        "scripts/module.py": "import pkgutil as p\nlib = p\n",
        "scripts/version.py": "import importlib.metadata\nread = importlib.metadata.version\n",
        "scripts/decode.py": "import importlib.util\nkeep(importlib.util.decode_source)\n",
        "scripts/called.py": 'from importlib.resources import files\nfiles("json")\n',
        "scripts/rebound.py": "from importlib.resources import files\nfiles = make()\nload = files\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("scripts.aliased", DYNAMIC): [2],
        ("scripts.unpacked", DYNAMIC): [2],
        ("scripts.passed", DYNAMIC): [2],
        ("scripts.module", DYNAMIC): [2],
    }
