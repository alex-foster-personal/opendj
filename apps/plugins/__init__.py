"""The plugin store: optional modules a user downloads into their data dir.

WHAT GOES HERE (PLUG-6522).  Features that are optional, experimental or bound by
third-party terms do not belong in the core bundle every user downloads.  They ship as
plugins: a signed archive listed in a signed catalog, installed into
``<data-dir>/plugins/<id>/<version>/``, enabled per user, and loaded at the
next engine start.  B2B Link is the first module this was built for.

TWO TRUST CLASSES, because ``docs/security/plugin-store-scanning.md`` already
decided what third-party code may do:

* ``first-party``: built and signed by Open DJ from this repository.  May
  carry Python, because it is the same code, reviewed the same way, that
  would otherwise be compiled into the core.  The signature is the only thing
  that lets Python load (PLUG-6522c).
* ``third-party``: ``kind="data"`` only (controller maps, themes, presets)
  until the sandbox in the scanning doc exists.  A third-party manifest that
  declares Python or UI code is refused at parse time, not at load time.

TWO CHANNELS, mapping the distribution tier rule (odj-private
issue #5) onto distribution:

* ``stable``: tier 0 and tier 1.
* ``labs``: experiments and tier 2 (works with outside services or
  content whose terms may limit use).  Hidden until the
  user opts in, every tier-2 entry carries a notice the user must see before
  installing, and labs modules are never enabled by default.
* Tier 3 is refused by the manifest parser.  It never appears in a
  public index.

WHERE THIS SITS.  A domain package.  ``apps.engine_core`` is the composition
root that loads enabled modules and mounts the HTTP surface
(``apps.engine_core.plugins_api``); this package never imports the web UI or
the engine.
"""

from __future__ import annotations

from apps.plugins.manifest import (
    CHANNELS,
    KINDS,
    TRUST_CLASSES,
    ManifestError,
    PluginManifest,
    parse_manifest,
)

__all__ = [
    "CHANNELS",
    "KINDS",
    "TRUST_CLASSES",
    "ManifestError",
    "PluginManifest",
    "parse_manifest",
]
