"""Compatibility facade for native-analysis column documentation.

Re-exports :data:`NATIVE_ANALYSIS_COLUMN_DOCS` from
:mod:`apps.shared.agents_md.column_docs_native_analysis`.

DDL for these tables lives in ``apps/analysis/native_tables_sql.py``
(``NATIVE_ANALYSIS_V1_SQL``), assembled into ``apps/analysis/store.py``
(``_ANALYSIS_TABLES_SQL``) and mirrored into the consolidated engine
schema's ``native_analysis_v1`` domain.
Design: ``(private path)`` section 3.
"""

from apps.shared.agents_md.column_docs_native_analysis import NATIVE_ANALYSIS_COLUMN_DOCS

__all__ = ["NATIVE_ANALYSIS_COLUMN_DOCS"]
