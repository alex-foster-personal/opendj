"""Whether a process id names a running process, the same answer on every OS.

INSTALL-14 names this module. The implementation is
``apps.shared.process_liveness.pid_is_alive`` (stdlib-only). This module
re-exports it as ``pid_is_running``.

``os.kill(pid, 0)`` is the POSIX idiom and a trap on Windows: there signal 0 is
``CTRL_C_EVENT``, so probing a live pid interrupts its console group.

Supersedes: the ``psutil.pid_exists`` body that previously lived here.
"""

from __future__ import annotations

from apps.shared.process_liveness import pid_is_alive as pid_is_running

__all__ = ["pid_is_running"]
