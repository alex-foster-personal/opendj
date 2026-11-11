"""Read direct JS/TS child_process spawns with tree-sitter.

Supersedes: regex-shaped prototypes that cannot see imports or call shape.
A literal argv is joined for the shell scanner; a dynamic argv stays UNKNOWN,
matching scripts/runner_toolset_shell_lex.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import tree_sitter_javascript as tsjavascript
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from scripts.runner_toolset_shell_lex import UNKNOWN, join_literal_argv

SINKS = frozenset(
    {
        "spawn",
        "spawnSync",
        "exec",
        "execFile",
        "execSync",
        "execFileSync",
    }
)
SHELL_SINKS = frozenset({"exec", "execSync"})
CHILD_MODULE_SUFFIXES = ("/child_process", "child_process")


@dataclass(frozen=True)
class Command:
    kind: str
    text: str
    line: int


def _strip_quotes(text: str) -> str | None:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return None


def _node_text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode()


@lru_cache(maxsize=4)
def _language(language_id: str) -> Language:
    if language_id == "typescript":
        return Language(tstypescript.language_typescript())
    if language_id == "tsx":
        return Language(tstypescript.language_tsx())
    return Language(tsjavascript.language())


def _language_id(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith(".tsx"):
        return "tsx"
    if lowered.endswith((".ts", ".mts", ".cts")):
        return "typescript"
    return "javascript"


def _child_module(source: bytes, node: Node) -> bool:
    text = _strip_quotes(_node_text(source, node))
    if text is None:
        return False
    return text == "child_process" or text.endswith(CHILD_MODULE_SUFFIXES)


def _literal_string(source: bytes, node: Node) -> str | None:
    if node.type != "string":
        return None
    return _strip_quotes(_node_text(source, node))


def _literal_array(source: bytes, node: Node) -> list[str] | None:
    if node.type != "array":
        return None
    words: list[str] = []
    for child in node.children:
        if child.type in {",", "[", "]"}:
            continue
        if child.type == "spread_element":
            return None
        word = _literal_string(source, child)
        if word is None:
            return None
        words.append(word)
    return words


@dataclass
class _Bindings:
    sinks: dict[str, str]

    def sink_name(self, name: str) -> str | None:
        bound = self.sinks.get(name)
        if bound in SINKS:
            return bound
        if bound == "execa":
            return "execa"
        return None

    def namespace(self, name: str) -> bool:
        return self.sinks.get(name) in {"child_process", "execa_module"}


class _ImportPass:
    def __init__(self, source: bytes):
        self.source = source
        self.bindings = _Bindings(sinks={})

    def walk(self, node: Node) -> None:
        if node.type == "import_statement":
            self._import(node)
        elif node.type in {"lexical_declaration", "variable_declaration"}:
            self._variables(node)
        for child in node.children:
            self.walk(child)

    def _import(self, node: Node) -> None:
        module = next((c for c in node.children if c.type == "string"), None)
        if module is None or not _child_module(self.source, module):
            if module is not None and _strip_quotes(_node_text(self.source, module)) == "execa":
                clause = next((c for c in node.children if c.type == "import_clause"), None)
                if clause is not None and clause.children and clause.children[0].type == "identifier":
                    self.bindings.sinks[_node_text(self.source, clause.children[0])] = "execa"
            return
        clause = next((c for c in node.children if c.type == "import_clause"), None)
        if clause is None:
            return
        if clause.children and clause.children[0].type == "identifier":
            self.bindings.sinks[_node_text(self.source, clause.children[0])] = "child_process"
            return
        named = next((c for c in clause.children if c.type == "named_imports"), None)
        if named is None:
            return
        for spec in named.children:
            if spec.type != "import_specifier":
                continue
            ids = [c for c in spec.children if c.type == "identifier"]
            if not ids:
                continue
            imported = ids[0]
            local = ids[1] if len(ids) > 1 else ids[0]
            if _node_text(self.source, imported) in SINKS:
                self.bindings.sinks[_node_text(self.source, local)] = _node_text(self.source, imported)

    def _variables(self, node: Node) -> None:
        for child in node.children:
            if child.type != "variable_declarator":
                continue
            self._declarator(child)

    def _declarator(self, node: Node) -> None:
        pattern = next((c for c in node.children if c.type == "object_pattern"), None)
        init = next((c for c in node.children if c.type == "call_expression"), None)
        if pattern is not None and init is not None:
            module = _require_module(self.source, init)
            if module == "child_process":
                for entry in pattern.children:
                    if entry.type == "shorthand_property_identifier_pattern":
                        sink = _node_text(self.source, entry)
                        if sink in SINKS:
                            self.bindings.sinks[sink] = sink
            elif module == "execa" and pattern.children:
                first = pattern.children[0]
                if first.type == "shorthand_property_identifier_pattern":
                    self.bindings.sinks[_node_text(self.source, first)] = "execa"
            return
        name = next((c for c in node.children if c.type == "identifier"), None)
        if name is None or init is None:
            return
        module = _require_module(self.source, init)
        if module is None:
            return
        local = _node_text(self.source, name)
        if module == "child_process":
            self.bindings.sinks[local] = "child_process"
        elif module == "execa":
            self.bindings.sinks[local] = "execa_module"


def _require_module(source: bytes, call: Node) -> str | None:
    callee = call.child_by_field_name("function")
    if callee is None or callee.type != "identifier":
        return None
    if _node_text(source, callee) != "require":
        return None
    args = call.child_by_field_name("arguments")
    if args is None or not args.children:
        return None
    module_node = next((c for c in args.children if c.type == "string"), None)
    if module_node is None:
        return None
    module = _strip_quotes(_node_text(source, module_node))
    if module in {"child_process", "node:child_process"}:
        return "child_process"
    if module == "execa":
        return "execa"
    return None


def _resolve_sink(source: bytes, callee: Node, bindings: _Bindings) -> str | None:
    if callee.type == "identifier":
        return bindings.sink_name(_node_text(source, callee))
    if callee.type != "member_expression":
        return None
    prop = callee.child_by_field_name("property")
    obj = callee.child_by_field_name("object")
    if prop is None or obj is None:
        return None
    method = _node_text(source, prop)
    if method not in SINKS and not (method == "spawn" and _node_text(source, obj) == "Bun"):
        return None
    if _node_text(source, obj) == "Bun" and method == "spawn":
        return "spawn"
    if obj.type == "identifier" and bindings.namespace(_node_text(source, obj)):
        return method
    return None


def _argv_from_call(source: bytes, call: Node, sink: str) -> list[str] | None:
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    arg_nodes = [c for c in args.children if c.type not in {",", "(", ")"}]
    if not arg_nodes:
        return None
    if sink in SHELL_SINKS:
        word = _literal_string(source, arg_nodes[0])
        return [word] if word is not None else None
    if sink == "execa":
        return _spawn_argv(source, arg_nodes)
    command = _literal_string(source, arg_nodes[0])
    if command is None:
        return None
    if len(arg_nodes) == 1:
        return [command]
    extra = _literal_array(source, arg_nodes[1])
    if extra is None:
        return None
    return [command, *extra]


def _spawn_argv(source: bytes, arg_nodes: list[Node]) -> list[str] | None:
    command = _literal_string(source, arg_nodes[0])
    if command is None:
        return None
    if len(arg_nodes) == 1:
        return [command]
    rest = _literal_array(source, arg_nodes[1])
    if rest is None:
        return None
    return [command, *rest]


def _commands_in_tree(source: bytes, root: Node, bindings: _Bindings) -> list[Command]:
    found: list[Command] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            if callee is not None:
                sink = _resolve_sink(source, callee, bindings)
                if sink is not None:
                    line = node.start_point[0] + 1
                    argv = _argv_from_call(source, node, sink)
                    if argv is None:
                        found.append(Command("unresolved-argv", UNKNOWN, line))
                    elif sink in SHELL_SINKS:
                        found.append(Command("shell", argv[0], line))
                    else:
                        found.append(Command("shell", join_literal_argv(argv), line))
        stack.extend(node.children)
    return found


def commands(text: str, *, path: str = "file.mjs") -> list[Command]:
    """Return literal shell argv lines and UNKNOWN markers from a JS/TS source."""
    source = text.encode()
    parser = Parser(_language(_language_id(path)))
    tree = parser.parse(source)
    bindings = _ImportPass(source)
    bindings.walk(tree.root_node)
    hits = _commands_in_tree(source, tree.root_node, bindings.bindings)
    return list(dict.fromkeys(hits))
