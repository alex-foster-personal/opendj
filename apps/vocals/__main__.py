"""``python -m apps.vocals`` entry point."""
from __future__ import annotations

import sys

from apps.vocals.cli import main

if __name__ == "__main__":
    sys.exit(main())
