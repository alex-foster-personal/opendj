"""Adapter registry for the ``open-dj-tool`` CLI.

The CLI accepts ``--adapter <name>`` and looks up this table to find a
uniform *AdapterSpec* describing how to drive both the *export* (vendor DB
-> open-dj JSON) and *import* (open-dj JSON -> vendor DB) paths.

Two adapter families coexist in the tree, for historical reasons:

* ``apps.open_dj.adapters.rekordbox`` and ``.djay`` expose a module-level
  :func:`export_library` (Phase 15) that takes a ``source_path`` and
  returns an :class:`apps.open_dj.adapters._base.ExportResult`. They do
  *not* ship an ``import_library`` entry-point yet; live writes still go
  through :mod:`apps.sync.apply_ratings` and :mod:`apps.sync.playlist_apply`
  under the Phase 4 6-rail safety harness. The registry flags these as
  ``import_supported=False`` and the CLI emits a follow-up hint.

* ``apps.adapters.serato.adapter.SeratoAdapter`` and
  ``apps.adapters.traktor.adapter.TraktorAdapter`` (Phase 16) implement
  the :class:`apps.open_dj.Adapter` Protocol with ``read`` / ``write``
  methods operating on :class:`apps.open_dj.OpenDjLibrary`. Both import
  and export are wired end-to-end.

All lookups go through :func:`load_adapter`; tests can monkeypatch
:data:`ADAPTERS` to inject fakes without touching the CLI.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Literal

AdapterFamily = Literal["module", "class"]


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    """Describes how to drive a given adapter from the CLI layer.

    ``family == "module"``: export goes through ``module.export_library``;
    import is not wired here yet (rekordbox / djay).

    ``family == "class"``: an adapter class implementing the
    :class:`apps.open_dj.Adapter` Protocol (``read`` / ``write``); import
    and export both supported (serato / traktor).
    """

    name: str
    family: AdapterFamily
    target_path: str  # dotted "module:attribute" pointer.
    export_supported: bool = True
    import_supported: bool = False
    safety_target: str | None = None  # "rekordbox" / "djay" for the 6-rail harness.


ADAPTERS: dict[str, AdapterSpec] = {
    "rekordbox": AdapterSpec(
        name="rekordbox",
        family="module",
        target_path="apps.open_dj.adapters.rekordbox",
        export_supported=True,
        # Import deferred: live writes must go through apps.sync.apply_ratings
        # (ratings) or apps.sync.playlist_apply (playlists) under LiveWriteSession.
        import_supported=False,
        safety_target="rekordbox",
    ),
    "djay": AdapterSpec(
        name="djay",
        family="module",
        target_path="apps.open_dj.adapters.djay",
        export_supported=True,
        import_supported=False,
        safety_target="djay",
    ),
    "serato": AdapterSpec(
        name="serato",
        family="class",
        target_path="apps.adapters.serato.adapter:SeratoAdapter",
        export_supported=True,
        import_supported=True,
        # Serato has its own safety layer in apps.adapters.serato.safety.
        safety_target=None,
    ),
    "traktor": AdapterSpec(
        name="traktor",
        family="class",
        target_path="apps.adapters.traktor.adapter:TraktorAdapter",
        export_supported=True,
        import_supported=True,
        safety_target=None,
    ),
}


class AdapterNotFoundError(KeyError):
    """Raised when ``--adapter <name>`` is not in :data:`ADAPTERS`."""


def available_adapters() -> list[str]:
    """Return the sorted list of adapter names the CLI will accept."""
    return sorted(ADAPTERS)


def load_adapter(name: str) -> Any:
    """Resolve ``name`` to a runnable adapter object.

    For ``family == "class"``, returns an instantiated adapter (no args).
    For ``family == "module"``, returns the imported module itself (caller
    invokes ``module.export_library(...)`` / etc.).
    """
    spec = get_spec(name)
    if spec.family == "class":
        module_path, _, class_name = spec.target_path.partition(":")
        if not class_name:
            raise ValueError(
                f"adapter {name!r}: class family requires 'module:Class' target_path"
            )
        module = importlib.import_module(module_path)
        return getattr(module, class_name)()
    # module family.
    return importlib.import_module(spec.target_path)


def get_spec(name: str) -> AdapterSpec:
    """Return the :class:`AdapterSpec` for ``name`` or raise."""
    try:
        return ADAPTERS[name]
    except KeyError as exc:
        raise AdapterNotFoundError(
            f"unknown adapter {name!r}; available: {', '.join(available_adapters())}"
        ) from exc
