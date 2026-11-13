"""The tree-read census measures always-run completeness at run time (#6526).

These prove the plugin records a real guard's listings and the report classifies them; the full
census itself is one suite-wide run, documented in `scripts/ci_tree_read_census.py`.

Split from this module (issue #10939) into sibling tests under 600 lines each::

- ``test_ci_tree_read_census_tree.py``
- ``test_ci_tree_read_census_report.py``
- ``test_ci_tree_read_census_startup.py``
- ``test_ci_tree_read_census_process.py``
- ``test_ci_tree_read_census_races.py``
- ``test_ci_tree_read_census_programs.py`` (child programs; split earlier)

Shared helpers: ``tree_read_census_support.py``.
"""
