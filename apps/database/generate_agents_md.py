"""Public CLI and compatibility facade for generated AGENTS.md documentation."""

from apps.shared.agents_md import generator as _generator
from apps.shared.agents_md.generator import (
    GENERATOR_VERSION,
    ColumnInfo,
    ExistingSidecarAccessError,
    ForeignKeyInfo,
    MissingColumnDocsError,
    SidecarReplacePermissionError,
    TableInfo,
    agents_md_cache_line,
    agents_md_cache_marker,
    introspect,
    main,
    render,
    schema_fingerprint,
    sidecar_carries_cache_line,
    write_agents_md,
)

ForeignAgentsMdError = _generator.ForeignAgentsMdError
_is_fts5_shadow_table = _generator._is_fts5_shadow_table
_virtual_table_names = _generator._virtual_table_names

__all__ = [
    "GENERATOR_VERSION",
    "ColumnInfo",
    "ExistingSidecarAccessError",
    "ForeignKeyInfo",
    "MissingColumnDocsError",
    "SidecarReplacePermissionError",
    "TableInfo",
    "agents_md_cache_line",
    "agents_md_cache_marker",
    "introspect",
    "main",
    "render",
    "schema_fingerprint",
    "sidecar_carries_cache_line",
    "write_agents_md",
]


if __name__ == "__main__":
    main()
