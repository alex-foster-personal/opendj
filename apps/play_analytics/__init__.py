"""Read-only projections over the canonical Phase 12 set event store.

Outside this package, import ``apps.play_analytics.contract`` (delivery layers
may also import ``apps.play_analytics.http``).
"""

from .contract import AnalyticsSchemaError

__all__ = ["AnalyticsSchemaError"]
