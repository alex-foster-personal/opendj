"""Pytest path for the vendored agents--transcript-hub sources."""

from __future__ import annotations

import sys
from pathlib import Path

_HUB_SRC = Path(__file__).resolve().parents[2] / "agents--transcript-hub" / "src"
if _HUB_SRC.is_dir():
    sys.path.insert(0, str(_HUB_SRC))
