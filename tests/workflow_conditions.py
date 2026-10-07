"""Reading GitHub Actions `if:` conditions, with nothing but the standard library.

Supersedes: ``top_level_disjuncts`` in ``tests/ci_cost_guard_workflow_reader.py``,
which now imports it from here.

Split out of tests/ci_cost_guard_workflow_reader.py so a suite that only needs
to read a condition does not import that reader, which imports
`scripts.ci_cost_guard` and would make the importing suite depend on the
`scripts` scope (SMARTEST-CI round 12, Codex on #3830).

This module holds no tests of its own and is deliberately not named `test_*`.
"""

from __future__ import annotations

import re


def top_level_disjuncts(condition: str) -> list[str]:
    """`condition` split on `||` at bracket depth zero.

    A plain `str.split("||")` would cut inside a parenthesized clause and hand
    back fragments, and a fragment reads as a weaker claim than the clause it
    came from - the same fail-open shape `event_set` refuses below.
    """
    stripped = re.sub(r"\s+", " ", condition).strip()
    parts, depth, start = [], 0, 0
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0 and stripped[index : index + 2] == "||":
            parts.append(stripped[start:index].strip())
            index += 2
            start = index
            continue
        index += 1
    parts.append(stripped[start:].strip())
    return [part for part in parts if part]
