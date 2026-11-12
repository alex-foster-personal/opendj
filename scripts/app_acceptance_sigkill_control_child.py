#!/usr/bin/env python3
"""Sigterm-ignoring child for installed-app acceptance (issue #11421).

Launched by scripts/app_acceptance.py before clean_quit. Stdlib only so it can
run beside a copied acceptance script with no project venv.
"""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path


def main() -> None:
    marker = Path(sys.argv[1])
    signal.signal(getattr(signal, "SIGBREAK", signal.SIGTERM), signal.SIG_IGN)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("opendj-app-acceptance-sigkill-control/v1\n", encoding="utf-8")
    time.sleep(3600)


if __name__ == "__main__":
    main()
