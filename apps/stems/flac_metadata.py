"""Backward import for the FLAC metadata seam.

Supersedes: apps/stems/flac_metadata.py -> apps/stems/artifacts_flac.py
"""

from apps.stems.artifacts_flac import (
    _flac_block_size_from_header,
    _flac_valid_frame_header_at,
    _require_flac_magic,
    read_flac_metadata,
)

__all__ = [
    "_flac_block_size_from_header",
    "_flac_valid_frame_header_at",
    "_require_flac_magic",
    "read_flac_metadata",
]
