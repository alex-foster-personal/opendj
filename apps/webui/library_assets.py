"""Stem root selection lives in ``apps.stems.storage``.

Re-exported so existing web UI callers keep their import path. The domain
package must not import this module: that direction breaks the layer contract.
"""

from apps.stems.storage import StemStorage, ensure_stem_storage, stem_storage

__all__ = ["StemStorage", "ensure_stem_storage", "stem_storage"]
