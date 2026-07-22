"""Read-only projections over the canonical Phase 12 set event store."""

from .query import AnalyticsSchemaError, query_play_analytics

__all__ = ["AnalyticsSchemaError", "query_play_analytics"]
