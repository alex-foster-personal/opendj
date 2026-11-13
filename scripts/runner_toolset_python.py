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
from pathlib import Path

from scripts.runner_toolset_shell_lex import PLACEHOLDER

PROCESS = {"run", "call", "check_call", "check_output", "Popen"}
ASYNC_PROCESS = {"create_subprocess_exec"}
PY_MODULE_PREFIXES = ("scripts.", "ops.", "apps.")
Value = tuple[tuple[str, ...], ...]
UNKNOWN: Value = ((PLACEHOLDER,),)
SYS_EXECUTABLE = "sys.executable"
HTTP_METHODS = frozenset({"GET", "PUT", "POST", "PATCH", "DELETE", "HEAD", "OPTIONS"})
LIMIT = 32


@dataclass(frozen=True)
class Command:
    kind: str
    text: str
    line: int
    chain: tuple[str, ...] = ()


def constant(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_path_constructor(func: ast.AST) -> bool:
    return (isinstance(func, ast.Name) and func.id == "Path") or (
        isinstance(func, ast.Attribute) and func.attr == "Path"
    )


def _eval_path(node: ast.AST, paths: dict[str, Path | None], source_file: Path | None) -> Path | None:
    """Resolve a module-level Path(__file__) chain when it is static."""
    if isinstance(node, ast.Name):
        return paths.get(node.id)
    if isinstance(node, ast.Call):
        if _is_path_constructor(node.func):
            if not node.args:
                return None
            if isinstance(node.args[0], ast.Name) and node.args[0].id == "__file__":
                return source_file
            return _eval_path(node.args[0], paths, source_file)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "resolve":
            base = _eval_path(node.func.value, paths, source_file)
            return base.resolve() if base else None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "fspath" and node.args:
            return _eval_path(node.args[0], paths, source_file)
        if isinstance(node.func, ast.Name) and node.func.id == "fspath" and node.args:
            return _eval_path(node.args[0], paths, source_file)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _eval_path(node.value, paths, source_file)
        return base.parent if base else None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        if node.value.attr != "parents":
            return None
        base = _eval_path(node.value.value, paths, source_file)
        if base is None:
            return None
        index = node.slice.value if isinstance(node.slice, ast.Constant) else None
        return base.parents[index] if isinstance(index, int) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _eval_path(node.left, paths, source_file)
        if left is None:
            return None
        if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            return left / node.right.value
        right = _eval_path(node.right, paths, source_file)
        return left / right if right else None
    return None


def _path_words(node: ast.AST, paths: dict[str, Path | None], source_file: Path | None) -> Value:
    path = _eval_path(node, paths, source_file)
    if path is None:
        return UNKNOWN
    return ((str(path),),)


def _repo_relative(path: Path, root: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    text = str(rel)
    return text if not text.startswith("..") else None


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


def _value(
    node: ast.AST,
    env: dict[str, Value],
    *,
    paths: dict[str, Path | None] | None = None,
    source_file: Path | None = None,
) -> Value:
    paths = paths or {}
    if (word := constant(node)) is not None:
        return ((word,),)
    if isinstance(node, ast.Name):
        if node.id in paths and paths[node.id] is not None:
            return ((str(paths[node.id]),),)
        return env.get(node.id, UNKNOWN)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sys":
        if node.attr == "executable":
            return ((SYS_EXECUTABLE,),)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str" and node.args:
        path_words = _path_words(node.args[0], paths, source_file)
        if path_words is not UNKNOWN:
            return path_words
    if (
        isinstance(node, ast.Call)
        and node.args
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "fspath")
            or (isinstance(node.func, ast.Name) and node.func.id == "fspath")
        )
    ):
        path_words = _path_words(node.args[0], paths, source_file)
        if path_words is not UNKNOWN:
            return path_words
    path_words = _path_words(node, paths, source_file)
    if path_words is not UNKNOWN:
        return path_words
    if isinstance(node, (ast.List, ast.Tuple)):
        return _combine(
            [
                _value(e.value if isinstance(e, ast.Starred) else e, env, paths=paths, source_file=source_file)
                for e in node.elts
            ]
        )
    if isinstance(node, ast.IfExp):
        return tuple(
            dict.fromkeys(
                (
                    *_value(node.body, env, paths=paths, source_file=source_file),
                    *_value(node.orelse, env, paths=paths, source_file=source_file),
                )
            )
        )[:LIMIT]
    return UNKNOWN


def _arguments(
    call: ast.Call,
    env: dict[str, Value],
    *,
    paths: dict[str, Path | None],
    source_file: Path | None,
) -> list[Value]:
    args: list[Value] = []
    for node in call.args:
        if isinstance(node, ast.Starred):
            rows = _value(node.value, env, paths=paths, source_file=source_file)
            # Uncertain spread arity cannot establish a local parameter binding.
            if len(rows) != 1:
                return []
            args.extend(((word,),) for word in rows[0])
        else:
            args.append(_value(node, env, paths=paths, source_file=source_file))
    return args


def _bind(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    call: ast.Call,
    env: dict[str, Value],
    globals_: dict[str, Value],
    *,
    paths: dict[str, Path | None],
    source_file: Path | None,
) -> dict[str, Value] | None:
    args = _arguments(call, env, paths=paths, source_file=source_file)
    names = [*function.args.posonlyargs, *function.args.args]
    if not function.args.vararg and len(args) > len(names):
        return None
    bound = dict(globals_)
    defaults = [None] * (len(names) - len(function.args.defaults)) + function.args.defaults
    keywords = {kw.arg: _value(kw.value, env, paths=paths, source_file=source_file) for kw in call.keywords if kw.arg}
    for index, (parameter, default) in enumerate(zip(names, defaults, strict=True)):
        bound[parameter.arg] = (
            args[index]
            if index < len(args)
            else keywords.get(
                parameter.arg,
                _value(default, env, paths=paths, source_file=source_file) if default else UNKNOWN,
            )
        )
    if function.args.vararg:
        bound[function.args.vararg.arg] = _combine(args[len(names) :])
    for parameter, default in zip(function.args.kwonlyargs, function.args.kw_defaults, strict=True):
        bound[parameter.arg] = keywords.get(
            parameter.arg,
            _value(default, env, paths=paths, source_file=source_file) if default else UNKNOWN,
        )
    return bound


def _imports(tree: ast.Module) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    modules: dict[str, str] = {}
    functions: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update((a.asname or a.name, a.name) for a in node.names if a.name in {"subprocess", "asyncio"})
        elif isinstance(node, ast.ImportFrom) and node.module in {"subprocess", "asyncio"}:
            allowed = PROCESS if node.module == "subprocess" else ASYNC_PROCESS
            functions.update((a.asname or a.name, (node.module, a.name)) for a in node.names if a.name in allowed)
    return modules, functions


class _Flow:
    def __init__(self, tree: ast.Module, *, source_file: Path | None, repo_root: Path | None = None):
        self.repo_root = repo_root
        definitions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.functions = {n.name: n for n in definitions if sum(d.name == n.name for d in definitions) == 1}
        self.modules, self.sinks = _imports(tree)
        self.source_file = source_file
        self.paths: dict[str, Path | None] = {}
        self.globals: dict[str, Value] = {}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if value is None:
                    continue
                for name in targets:
                    if isinstance(name, ast.Name):
                        self.paths[name.id] = _eval_path(value, self.paths, source_file)
                        self.globals[name.id] = _value(value, self.globals, paths=self.paths, source_file=source_file)
        self.results: list[Command] = []

    def trace(
        self,
        call: ast.Call,
        env: dict[str, Value],
        origin: int,
        chain: tuple[str, ...] = (),
        active: frozenset[str] = frozenset(),
    ) -> None:
        owner, name = target(call)
        module, underlying = (self.modules.get(owner, ""), name) if owner else self.sinks.get(name, ("", ""))
        allowed = PROCESS if module == "subprocess" else ASYNC_PROCESS if module == "asyncio" else set()
        if underlying in allowed and (owner or name) not in env:
            if not call.args:
                return
            executable = next((kw.value for kw in call.keywords if kw.arg == "executable"), None)
            values = (
                _combine(
                    [
                        _value(
                            n.value if isinstance(n, ast.Starred) else n,
                            env,
                            paths=self.paths,
                            source_file=self.source_file,
                        )
                        for n in call.args
                    ]
                )
                if module == "asyncio"
                else _value(call.args[0], env, paths=self.paths, source_file=self.source_file)
            )
            sink = f"{module}.{underlying}@{call.lineno}(alias:{owner or name})"
            provenance = (*chain, sink) if chain else ()
            overrides = (
                _value(executable, env, paths=self.paths, source_file=self.source_file)
                if executable is not None
                else ((),)
            )
            for argv, override in itertools.product(values, overrides):
                if executable is not None:
                    if not override or PLACEHOLDER in override or SYS_EXECUTABLE in override:
                        self.results.append(
                            Command("unresolved-executable", ast.unparse(executable), origin, (*chain, sink))
                        )
                        continue
                    argv = (*override, *argv[1:])
                if argv and argv[0] == SYS_EXECUTABLE:
                    argv = ("python", *argv[1:])
                if argv and argv[0] in HTTP_METHODS:
                    continue
                if self.repo_root and argv:
                    try:
                        first = Path(argv[0])
                        if first.is_absolute():
                            rel = str(first.resolve().relative_to(self.repo_root.resolve()))
                            if (self.repo_root / rel).is_file():
                                continue
                    except (ValueError, OSError):
                        pass
                if argv and argv[0] != PLACEHOLDER:
                    shell = shlex.join(argv)
                    if len(argv) == 1 and any(
                        k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                        for k in call.keywords
                    ):
                        shell = argv[0]
                    self.results.append(Command("shell", shell, origin, provenance))
        elif not owner and name in self.functions and name not in active and len(active) < 12:
            function = self.functions[name]
            bound = _bind(function, call, env, self.globals, paths=self.paths, source_file=self.source_file)
            if bound is not None:
                self.walk(
                    function.body,
                    bound,
                    origin,
                    (*chain, f"{name}@{call.lineno}(definition:{function.lineno})"),
                    active | {name},
                )

    def walk(
        self,
        statements: list[ast.stmt],
        env: dict[str, Value],
        origin: int,
        chain: tuple[str, ...],
        active: frozenset[str],
    ) -> None:
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
                        if node.func.value.id in env and node.func.attr in {
                            "append",
                            "extend",
                            "insert",
                            "clear",
                            "pop",
                            "remove",
                        }:
                            env[node.func.value.id] = UNKNOWN
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target_ in targets:
                    if isinstance(target_, ast.Name) and statement.value is not None:
                        self.paths[target_.id] = _eval_path(statement.value, self.paths, self.source_file)
                        env[target_.id] = _value(statement.value, env, paths=self.paths, source_file=self.source_file)


def _module_paths(tree: ast.Module, source_file: Path | None) -> dict[str, Path | None]:
    paths: dict[str, Path | None] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    paths[target.id] = _eval_path(value, paths, source_file)
    return paths


def repo_queue_targets(text: str, source: str, repo_root: Path) -> list[str]:
    """Repo scripts a test may subprocess-run via resolved module-level Path argv."""
    from scripts import runner_toolset_sources as sources

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    source_file = (repo_root / source).resolve() if source.endswith(".py") else None
    paths = _module_paths(tree, source_file)
    queued: list[str] = []
    seen: set[str] = set()

    def note_path(path: Path) -> None:
        rel = _repo_relative(path, repo_root) or sources.repo_script_for(str(path), source, "", repo_root)
        if rel and rel not in seen and (repo_root / rel).is_file():
            seen.add(rel)
            queued.append(rel)

    def note_token(token: str) -> None:
        rel = sources.repo_script_for(token, source, "", repo_root)
        if rel:
            note_path(repo_root / rel)
        elif Path(token).is_absolute():
            note_path(Path(token))

    def walk_argv(node: ast.AST, env: dict[str, Path | None]) -> None:
        if isinstance(node, (ast.List, ast.Tuple)):
            elts = [e.value if isinstance(e, ast.Starred) else e for e in node.elts]
        else:
            elts = [node]
        argv: list[str] = []
        for elt in elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                argv.append(elt.value)
            elif isinstance(elt, ast.Name) and env.get(elt.id):
                argv.append(str(env[elt.id]))
            elif (
                isinstance(elt, ast.Attribute)
                and isinstance(elt.value, ast.Name)
                and elt.value.id == "sys"
                and elt.attr == "executable"
            ):
                argv.append(SYS_EXECUTABLE)
            elif isinstance(elt, ast.Call) and isinstance(elt.func, ast.Name) and elt.func.id == "str" and elt.args:
                path = _eval_path(elt.args[0], env, source_file)
                argv.append(str(path) if path else "")
            else:
                continue
        if not any(argv):
            return
        if argv[0] == SYS_EXECUTABLE:
            argv = ["python", *argv[1:]]
        if len(argv) >= 3 and argv[0] == "python" and argv[1] == "-m" and argv[2].startswith(PY_MODULE_PREFIXES):
            base = repo_root / Path(*argv[2].split("."))
            for candidate in (base.with_suffix(".py"), base / "__main__.py", base / "__init__.py"):
                if candidate.is_file():
                    rel = str(candidate.relative_to(repo_root))
                    if rel not in seen:
                        seen.add(rel)
                        queued.append(rel)
                    break
        for token in argv:
            note_token(token)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        owner, name = target(node)
        if name not in PROCESS and not (name in ASYNC_PROCESS and owner == "asyncio"):
            continue
        if name in PROCESS and owner not in {"subprocess", "sp", ""}:
            continue
        walk_argv(node.args[0], paths)

    return queued


def commands(text: str, *, source: str = "", repo_root: Path | None = None) -> list[Command]:
    tree = ast.parse(text)
    source_file = None
    if source.endswith(".py") and repo_root is not None:
        source_file = (repo_root / source).resolve()
    flow = _Flow(tree, source_file=source_file, repo_root=repo_root)
    direct: list[Command] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        owner, name = target(node)
        word = constant(node.args[0])
        if name == "which" and owner in {"shutil", ""} and word:
            direct.append(Command("name", word, node.lineno))
        elif (name in PROCESS and owner in {"subprocess", "sp", ""}) or (name in ASYNC_PROCESS and owner == "asyncio"):
            if not any(kw.arg == "executable" for kw in node.keywords) and (shell := literal_shell(node)):
                direct.append(Command("shell", shell, node.lineno))
        flow.trace(node, flow.globals, node.lineno)
    return list(dict.fromkeys([*direct, *flow.results]))
