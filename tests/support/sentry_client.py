"""Tear down a test's real sentry_sdk client.

``sentry_sdk.init(dsn=None)`` only swaps in a new client: the old one is never
closed, so its ``sentry.monitor`` thread keeps looping ``time.sleep(10)`` for
the rest of the pytest process. Close the live client (which kills its
monitor, session flusher, batchers and transport), then put the global scope
back on the SDK's never-initialised NonRecordingClient.

-Claude
"""
from __future__ import annotations


def close_sentry_client() -> None:
    """Close the global sentry_sdk client and restore the never-initialised state."""
    import sentry_sdk

    scope = sentry_sdk.get_global_scope()
    scope.client.close()
    scope.set_client(None)
