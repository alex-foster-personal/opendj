"""Module execution hook for ``python -m apps.playlist_sets`` (SET-05)."""

from __future__ import annotations

from apps.shared.playlist_sets.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
