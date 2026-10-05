"""What the R2 stem index says about bundles that are not on this disk.

Stem eviction (STEM-39..43) removes a local bundle only after R2 is proven to
hold it byte for byte, and the deck fetches it back on demand in seconds. A
coverage reader that counts only local bundles therefore reports every evicted
track as "needs the farm", which is false: nothing needs rendering. This
module is the one place coverage asks "is this bundle in the cloud, and can
this machine get it back?".

Three answers, never two:

=========  =================================================================
state      meaning
=========  =================================================================
ok         a hydration source is armed and the cached index was read. A
           bundle the index lists with a manifest counts as in cloud
off        no hydration source on this machine (local mode). Nothing is in
           cloud here; a missing bundle is simply pending
unknown    hydration is configured but not armed, or the index cache is
           absent or unreadable. Nothing is counted as in cloud, and the
           lights that depend on the answer render grey
=========  =================================================================

Reads only the local cache copy of the index, memoized on the file's
mtime and size (``stem_index.load_cached_index_memo``): no network, and a
stat per call once warm.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 HEALTH-07 an unknown index is never read as "in cloud"
    [if] the index cache file is corrupt [then] state unknown, holds() False
    [if] hydration is armed and no index was ever fetched [then] unknown
    [if] hydration is configured but could not arm [then] unknown
  ✔︎ ✅ 🎯 HEALTH-07 in cloud means fetchable
    [if] no hydration source is armed [then] state off, holds() False
    [if] the index lists a bundle without a manifest [then] holds() False
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from apps.cloud import stem_index

CloudState = Literal["ok", "off", "unknown"]


@dataclass(frozen=True)
class StemCloud:
    state: CloudState
    reason: str | None = None
    index: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def holds(self, stable_id: str) -> bool:
        """True when this machine can fetch a complete bundle for the track."""
        if self.state != "ok":
            return False
        return stem_index.MANIFEST_FILENAME in (self.index.get(stable_id) or {})

    def as_dict(self) -> dict[str, str | None]:
        return {"state": self.state, "reason": self.reason}


OFF: StemCloud = StemCloud("off")


def read_stem_cloud(
    data_dir: Path, *, fetchable: bool, unarmed_reason: str | None
) -> StemCloud:
    if unarmed_reason is not None:
        return StemCloud("unknown", f"stem hydration is not armed: {unarmed_reason}")
    if not fetchable:
        return OFF
    if not stem_index.local_index_cache_path(data_dir).is_file():
        detail = stem_index.refresh_error(data_dir)
        return StemCloud(
            "unknown",
            "the R2 stem index has not been fetched on this machine"
            + (f" (last refresh failed: {detail})" if detail else ""),
        )
    try:
        index = stem_index.load_cached_index_memo(data_dir)
    except (stem_index.StemIndexError, OSError) as error:
        return StemCloud("unknown", f"the R2 stem index cache cannot be read: {error}")
    return StemCloud("ok", None, index)


def for_app_state(state: Any, data_dir: Path) -> StemCloud:
    """The cloud view for an app, from the attributes stem hydration binds."""
    return read_stem_cloud(
        data_dir,
        fetchable=getattr(state, "stem_hydration_source", None) is not None,
        unarmed_reason=getattr(state, "stem_hydration_unarmed_reason", None),
    )


__all__ = ["OFF", "CloudState", "StemCloud", "for_app_state", "read_stem_cloud"]
