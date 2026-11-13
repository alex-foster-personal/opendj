"""Tests for scripts/skill_tree_lint.py, each measurement proven in both directions.

Every fixture is a real directory of real markdown built in tmp_path and read
through the production scanner. A measurement that only ever reports its happy
value is not a measurement, so each one gets a case that must move it and a
control that must leave it alone.

Split into sibling modules in this directory:
``test_skill_tree_lint_file_refs.py``, ``test_skill_tree_lint_skill_graph.py``,
``test_skill_tree_lint_remote_destinations.py``, and
``test_skill_tree_lint_cycles_rules_gate.py``. Shared helpers live in
``skill_tree_lint_support.py``.
"""
