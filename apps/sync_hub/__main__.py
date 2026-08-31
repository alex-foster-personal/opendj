"""``python -m apps.sync_hub`` -- the operator CLI in :mod:`apps.sync_hub.maintenance`."""
from __future__ import annotations

import sys

from apps.sync_hub.maintenance import main

if __name__ == "__main__":
    sys.exit(main())
