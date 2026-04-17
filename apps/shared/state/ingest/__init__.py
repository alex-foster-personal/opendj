"""Ingest adapters for the shared state layer.

Each submodule wires a vendor DB (or filesystem) into
:class:`apps.shared.state.writer.StateWriter`. Ingest is always one-way
(vendor -> state); the write-back side lives in apps.reconcile / apps.sync.
"""
