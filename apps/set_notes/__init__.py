"""Set notes (SETNOTE-6340..6340f): one markdown note per scope string in state.db.

Surfaces: the HTTP router in :mod:`.http` (mounted by the web UI) and the CLI
``python -m apps.set_notes`` in :mod:`.cli`, over the same store.
"""
