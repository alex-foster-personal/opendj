"""Arrange filesystem timestamps that a fast machine will not arrange for you.

File timestamps do not come from the clock `time.time()` reads. `time.time()`
reads CLOCK_REALTIME; the kernel stamps inodes from its COARSE clock, which
only advances once per timer tick and can sit a whole tick behind. Two things
a test cannot assume follow from that, however obvious they look:

  - an mtime taken after a `time.time()` cutoff is greater than that cutoff,
  - two operations separated by microseconds land on different timestamps.

Both held on the GitHub-hosted runners, by the accident of those runners being
slow, and four tests broke the first time this suite ran on self-hosted
hardware (agentbox, Thu 3 Sep 2026): a whole write-restat cycle finished
inside one tick, so a restored mtime came out equal to the ctime that was
supposed to distinguish it, and an append landed 0.18ms BEHIND the cutoff it
was made after.

Nothing here relaxes what those tests assert. It arranges the precondition
they assert ON, so a failure means the code is wrong rather than the machine
is quick, and a filesystem whose clock genuinely never moves fails loudly with
its own message instead of hanging or going green.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

#: Generous against any plausible tick (CONFIG_HZ=100 is 10ms) and short
#: enough that a stuck clock reddens a test rather than stalling a CI job.
_DEADLINE_S = 5.0
_POLL_S = 0.002


def stamp_until(
    path: Path,
    satisfied: Callable[[os.stat_result], bool],
    what: str,
    *,
    times_ns: tuple[int, int] | None = None,
) -> os.stat_result:
    """Re-stamp ``path`` until the filesystem agrees ``satisfied``, or fail.

    ``times_ns`` is passed straight to :func:`os.utime`, so the caller keeps
    control of the field it is holding STILL: passing the original
    ``(atime_ns, mtime_ns)`` re-asserts a restored mtime on every attempt while
    ctime advances, which is the shape the ctime tests need. Omitting it stamps
    both to now, which is the shape "this file was modified after X" needs.

    Returns the stat that satisfied the predicate, so callers assert on the
    same reading the loop accepted rather than re-statting into a new race.
    """
    deadline = time.monotonic() + _DEADLINE_S
    while True:
        stat = path.stat()
        if satisfied(stat):
            return stat
        assert time.monotonic() < deadline, (
            f"{path} never reached the state this test needs ({what}) within "
            f"{_DEADLINE_S}s of re-stamping it. The filesystem clock is not "
            "advancing, so the precondition cannot be arranged here at all."
        )
        time.sleep(_POLL_S)
        if times_ns is None:
            os.utime(path)
        else:
            os.utime(path, ns=times_ns)
