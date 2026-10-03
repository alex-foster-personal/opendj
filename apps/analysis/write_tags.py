"""CLI entry that used to write BPM and key into audio files.

That write path depended on ``mutagen`` (GPL-2.0-or-later). This Apache-2.0
product does not depend on it. ``python -m apps.analysis.write_tags`` exits
2 and does not modify any file.
"""
from __future__ import annotations

import sys

from apps.shared.tag_writer import TAG_WRITE_REMOVED, TagWriteRemoved

log_name = "apps.analysis.write_tags"


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise TagWriteRemoved(TAG_WRITE_REMOVED)


_write_tags = _refuse
_atomic_write_tags = _refuse
apply_writes = _refuse


def main(argv: list[str] | None = None) -> int:
    """Print the removal reason and exit 2. ``argv`` is ignored."""
    del argv
    print(TAG_WRITE_REMOVED, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
