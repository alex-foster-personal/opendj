"""INFRA-8304: every ``library.changed`` kind is declared on both sides.

The browser drops (and logs as contract drift) any ``library.changed`` kind
missing from ``LIBRARY_KINDS`` in events-bus.ts. ``plays`` (a recorded play),
``transition_in``, ``cloudsync_policy`` and ``cloudsync_playlist_pin`` were
published with no entry there, so every play logged
``[events-bus] library.changed carries unknown kind 'plays'``.

  - if a publish site emits a kind missing from LIBRARY_CHANGED_KINDS then broken
  - if LIBRARY_CHANGED_KINDS and the frontend LIBRARY_KINDS differ then broken
  - if the kind scan finds no publish sites then the scan itself is broken

-Claude
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from apps.engine_core.ws import LIBRARY_CHANGED_KINDS, TOPIC_LIBRARY_CHANGED

REPO = Path(__file__).resolve().parents[2]
EVENTS_BUS_TS = REPO / "apps/webui/frontend/src/lib/api/events-bus.ts"


class _Consts:
    """A module's top-level string constants and the string values of its dicts."""

    def __init__(self, tree: ast.Module) -> None:
        self.names: dict[str, str] = {}
        self.dict_values: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            else:
                continue
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                self.names.update({t.id: value.value for t in targets if isinstance(t, ast.Name)})
            elif isinstance(value, ast.Dict):
                self.dict_values += [
                    v.value for v in value.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
                ]
        # A helper's ``kind`` parameter (routes/library_org.py ``_written``):
        # the string literals its in-module callers pass for that parameter.
        self.param_values: dict[str, list[str]] = {}
        params = {fn.name: [a.arg for a in fn.args.args] for fn in tree.body if isinstance(fn, ast.FunctionDef)}
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in params):
                continue
            passed = list(zip(params[call.func.id], call.args, strict=False))
            passed += [(k.arg, k.value) for k in call.keywords if k.arg is not None]
            for name, arg in passed:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    self.param_values.setdefault(name, []).append(arg.value)


def _kind_values(kind: ast.expr, consts: _Consts) -> list[str]:
    """The kinds one publish payload's ``kind`` expression can take."""
    if isinstance(kind, ast.Constant) and isinstance(kind.value, str):
        return [kind.value]
    if isinstance(kind, ast.Name) and kind.id in consts.names:
        return [consts.names[kind.id]]
    if isinstance(kind, ast.Name) and kind.id in consts.param_values:
        return sorted(set(consts.param_values[kind.id]))
    # A loop variable over a module-level dict of kinds (cloudsync policy).
    if isinstance(kind, ast.Name) and consts.dict_values:
        return consts.dict_values
    raise AssertionError(f"unresolvable library.changed kind expression: {ast.dump(kind)}")


def _published_kinds() -> dict[str, set[str]]:
    """kind -> files that publish it, from every ``publish("library.changed", {...})``."""
    found: dict[str, set[str]] = {}
    for path in sorted((REPO / "apps").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if TOPIC_LIBRARY_CHANGED not in text:
            continue
        tree = ast.parse(text)
        consts = _Consts(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and len(node.args) >= 2):
                continue
            topic, payload = node.args[0], node.args[1]
            if not (isinstance(topic, ast.Constant) and topic.value == TOPIC_LIBRARY_CHANGED):
                continue
            if not isinstance(payload, ast.Dict):
                continue
            for key, value in zip(payload.keys, payload.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "kind":
                    for kind in _kind_values(value, consts):
                        found.setdefault(kind, set()).add(str(path.relative_to(REPO)))
    return found


def _frontend_kinds() -> tuple[str, ...]:
    text = EVENTS_BUS_TS.read_text(encoding="utf-8")
    match = re.search(r"export const LIBRARY_KINDS = \[(.*?)\] as const;", text, re.S)
    assert match is not None, "LIBRARY_KINDS literal not found in events-bus.ts"
    return tuple(re.findall(r"'([a-z_]+)'", match.group(1)))


def test_every_published_kind_is_declared() -> None:
    published = _published_kinds()
    assert {"tracks", "plays", "beatgrid_edit"} <= published.keys(), published
    undeclared = {k: sorted(v) for k, v in published.items() if k not in LIBRARY_CHANGED_KINDS}
    assert undeclared == {}, f"add these kinds to LIBRARY_CHANGED_KINDS and LIBRARY_KINDS: {undeclared}"


def test_backend_and_frontend_kind_lists_match() -> None:
    assert _frontend_kinds() == LIBRARY_CHANGED_KINDS


def test_plays_kind_is_declared() -> None:
    assert "plays" in LIBRARY_CHANGED_KINDS
    assert "plays" in _frontend_kinds()
