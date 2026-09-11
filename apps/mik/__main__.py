"""``python -m apps.mik`` entry point. See :mod:`apps.mik.cli`."""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
