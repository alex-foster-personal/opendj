"""Semgrep positive control for odj-image-open-without-formats. Never imported.

scripts/security/scan_sast.sh scans this directory and fails as UNKNOWN when the
`ruleid` line below produces no finding. Normal scans exclude tests/fixtures/security/.
"""

import io

from PIL import Image


def decode_unrestricted(data: bytes) -> str | None:
    # ruleid: odj-image-open-without-formats
    return Image.open(io.BytesIO(data)).format


def decode_restricted(data: bytes) -> str | None:
    # ok: odj-image-open-without-formats
    return Image.open(io.BytesIO(data), formats=["PNG"]).format
