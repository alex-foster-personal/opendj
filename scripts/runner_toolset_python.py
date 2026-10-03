"""Read direct Python process calls and prove same-source argv forwarding.

Supersedes: the Python argv reader embedded in runner_toolset_scan.py.
Wrapper names carry no meaning. A literal command is reported only when its
arguments reach an imported subprocess sink through local parameters/aliases.
Unknown values remain placeholders; recursion and alternative expansion are bounded.
"""
from __future__ import annotations

import ast
import itertools
import shlex
from dataclasses import dataclass

from scripts.runner_toolset_shell_lex import PLACEHOLDER

PROCESS = {"run", "call", "check_call", "check_output", "Popen"}
ASYNC_PROCESS = {"create_subprocess_exec"}
Value = tuple[tuple[str, ...], ...]
UNKNOWN: Value = ((PLACEHOLDER,),)
LIMIT = 32


@dataclass(frozen=True)
class Command:
    kind: str
    text: str
    line: int
    chain: tuple[str, ...] = ()


def constant(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def target(node: ast.Call) -> tuple[str, str]:
    func = node.func
    if isinstance(func, ast.Attribute):
        return (func.value.id if isinstance(func.value, ast.Name) else "?"), func.attr
    return "", func.id if isinstance(func, ast.Name) else ""


def literal_shell(node: ast.Call) -> str | None:
    first = node.args[0]
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts and constant(first.elts[0]):
        return shlex.join([constant(e) or PLACEHOLDER for e in first.elts])
    if constant(first) and any(kw.arg == "shell" for kw in node.keywords):
        return constant(first)
    if target(node)[1] == "create_subprocess_exec":
        return constant(first)
    return None


def _combine(values: list[Value]) -> Value:
    combinations = itertools.islice(itertools.product(*values), LIMIT + 1)
    rows = tuple(tuple(word for vector in row for word in vector) for row in combinations)
    return rows if len(rows) <= LIMIT else UNKNOWN


def _value(node: ast.AST, env: dict[str, Value]) -> Value:
    if (word := constant(node)) is not None:
        return ((word,),)
    if isinstance(node, ast.Name):
        return env.get(node.id, UNKNOWN)
    if isinstance(node, (ast.List, ast.Tuple)):
        return _combine([_value(e.value if isinstance(e, ast.Starred) else e, env) for e in node.elts])
    if isinstance(node, ast.IfExp):
        return tuple(dict.fromkeys((*_value(node.body, env), *_value(node.orelse, env))))[:LIMIT]
    return UNKNOWN


def _arguments(call: ast.Call, env: dict[str, Value]) -> list[Value]:
    args: list[Value] = []
    for node in call.args:
        if isinstance(node, ast.Starred):
            rows = _value(node.value, env)
            # Uncertain spread arity cannot establish a local parameter binding.
            if len(rows) != 1:
                return []
            args.extend(((word,),) for word in rows[0])
        else:
            args.append(_value(node, env))
    return args


def _bind(function: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call,
          env: dict[str, Value], globals_: dict[str, Value]) -> dict[str, Value] | None:
    args = _arguments(call, env)
    names = [*function.args.posonlyargs, *function.args.args]
    if not function.args.vararg and len(args) > len(names):
        return None
    bound = dict(globals_)
    defaults = [None] * (len(names) - len(function.args.defaults)) + function.args.defaults
    keywords = {kw.arg: _value(kw.value, env) for kw in call.keywords if kw.arg}
    for index, (parameter, default) in enumerate(zip(names, defaults, strict=True)):
        bound[parameter.arg] = args[index] if index < len(args) else keywords.get(
            parameter.arg, _value(default, env) if default else UNKNOWN)
    if function.args.vararg:
        bound[function.args.vararg.arg] = _combine(args[len(names):])
    for parameter, default in zip(function.args.kwonlyargs, function.args.kw_defaults, strict=True):
        bound[parameter.arg] = keywords.get(parameter.arg, _value(default, env) if default else UNKNOWN)
    return bound


def _imports(tree: ast.Module) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    modules: dict[str, str] = {}
    functions: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update((a.asname or a.name, a.name) for a in node.names
                           if a.name in {"subprocess", "asyncio"})
        elif isinstance(node, ast.ImportFrom) and node.module in {"subprocess", "asyncio"}:
            allowed = PROCESS if node.module == "subprocess" else ASYNC_PROCESS
            functions.update((a.asname or a.name, (node.module, a.name)) for a in node.names
                             if a.name in allowed)
    return modules, functions


class _Flow:
    def __init__(self, tree: ast.Module):
        definitions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.functions = {n.name: n for n in definitions if sum(d.name == n.name for d in definitions) == 1}
        self.modules, self.sinks = _imports(tree)
        self.globals: dict[str, Value] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for name in node.targets:
                    if isinstance(name, ast.Name):
                        self.globals[name.id] = _value(node.value, self.globals)
        self.results: list[Command] = []

    def trace(self, call: ast.Call, env: dict[str, Value], origin: int,
              chain: tuple[str, ...] = (), active: frozenset[str] = frozenset()) -> None:
        owner, name = target(call)
        module, underlying = (self.modules.get(owner, ""), name) if owner else self.sinks.get(name, ("", ""))
        allowed = PROCESS if module == "subprocess" else ASYNC_PROCESS if module == "asyncio" else set()
        if chain and underlying in allowed and (owner or name) not in env:
            if not call.args:
                return
            executable = next((kw.value for kw in call.keywords if kw.arg == "executable"), None)
            values = (_combine([_value(n.value if isinstance(n, ast.Starred) else n, env)
                                for n in call.args]) if module == "asyncio"
                      else _value(call.args[0], env))
            for argv in values:
                if executable is not None:
                    override = _value(executable, env)
                    if len(override) != 1 or override[0] == (PLACEHOLDER,):
                        continue
                    argv = (*override[0], *argv[1:])
                if argv and argv[0] != PLACEHOLDER:
                    shell = shlex.join(argv)
                    if len(argv) == 1 and any(k.arg == "shell" and isinstance(k.value, ast.Constant)
                                              and k.value.value is True for k in call.keywords):
                        shell = argv[0]
                    self.results.append(Command("shell", shell, origin,
                                                (*chain, f"{module}.{underlying}@{call.lineno}(alias:{owner or name})")))
        elif not owner and name in self.functions and name not in active and len(active) < 12:
            function = self.functions[name]
            bound = _bind(function, call, env, self.globals)
            if bound is not None:
                self.walk(function.body, bound, origin, (*chain, f"{name}@{call.lineno}(definition:{function.lineno})"),
                          active | {name})

    def walk(self, statements: list[ast.stmt], env: dict[str, Value], origin: int,
             chain: tuple[str, ...], active: frozenset[str]) -> None:
        for statement in statements:
            # Nested definitions are separate scopes, not executed forwarding.
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            branches = [getattr(statement, name, []) for name in ("body", "orelse", "finalbody")]
            if any(branches):
                branch_envs = []
                for branch in branches:
                    copied = dict(env)
                    self.walk(branch, copied, origin, chain, active)
                    branch_envs.append(copied)
                for key in env:
                    if any(copied.get(key) != env[key] for copied in branch_envs):
                        env[key] = UNKNOWN
                continue
            for node in ast.walk(statement):
                if isinstance(node, ast.Call):
                    self.trace(node, env, origin, chain, active)
                    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                        if node.func.value.id in env and node.func.attr in {"append", "extend", "insert", "clear", "pop", "remove"}:
                            env[node.func.value.id] = UNKNOWN
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target_ in targets:
                    if isinstance(target_, ast.Name) and statement.value is not None:
                        env[target_.id] = _value(statement.value, env)


def commands(text: str) -> list[Command]:
    tree = ast.parse(text)
    flow = _Flow(tree)
    direct: list[Command] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        owner, name = target(node)
        word = constant(node.args[0])
        if name == "which" and owner in {"shutil", ""} and word:
            direct.append(Command("name", word, node.lineno))
        elif ((name in PROCESS and owner in {"subprocess", "sp", ""})
              or (name in ASYNC_PROCESS and owner == "asyncio")):
            if not any(kw.arg == "executable" for kw in node.keywords) and (shell := literal_shell(node)):
                direct.append(Command("shell", shell, node.lineno))
        flow.trace(node, flow.globals, node.lineno)
    return list(dict.fromkeys([*direct, *flow.results]))
