"""Count what a request really asks sqlite, on the REAL connections.

Two instruments, neither a mock -- every statement still runs against the
real database file; these only observe:

* :func:`trace_sqlite` observes every connection the process opens without
  replacing anything: ``sqlite3.connect`` stays the real function. CPython's
  ``sqlite3.connect/handle`` audit event (``sys.addaudithook``, 3.11+) hands
  over each new connection, but fires INSIDE ``Connection.__init__``, before
  the connection accepts ``set_trace_callback``. So the hook arms a one-shot
  profiler (``sys.setprofile``) on the connecting thread, and that attaches
  the trace at the thread's next interpreter event - after ``__init__``
  returns and before any statement can run, since running one takes a call.
  Each executed statement is recorded with the name of the thread that ran
  it. It also counts the connections themselves, which is what #3962 was:
  one fresh read-only connection per listed row.
* :class:`RowCounter` is a ``row_factory`` that counts every row a cursor
  hands back to Python on one connection (the playlist store's), which is
  what #3963 was: every existing member materialized several times per add.

Both expose a ``reset()`` so a test can measure one request in isolation
after fixture setup has run through the same connections.
"""
from __future__ import annotations

import re
import sqlite3
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, Literal


@dataclass
class SqlTrace:
    """Statements executed and connections opened while tracing is active."""

    #: ``(thread name, expanded SQL)`` per executed statement.
    statements: list[tuple[str, str]] = field(default_factory=list)
    #: Thread name per connection opened.
    connections: list[str] = field(default_factory=list)
    #: Thread name per connection closed before its trace could attach, so
    #: its statements (if any) went unseen. A measurement must see none.
    untraced: list[str] = field(default_factory=list)

    def reset(self) -> None:
        # clear() in place: already-open connections append to THESE lists.
        self.statements.clear()
        self.connections.clear()
        self.untraced.clear()

    def statements_outside(self, thread_name: str) -> list[str]:
        return [sql for name, sql in self.statements if name != thread_name]

    def connections_outside(self, thread_name: str) -> int:
        return sum(1 for name in self.connections if name != thread_name)

    def untraced_outside(self, thread_name: str) -> list[str]:
        return [name for name in self.untraced if name != thread_name]


def statement_shapes(statements: list[str]) -> Counter[str]:
    """Statements with literals blanked, so one query over different ids
    counts as one shape (the trace reports SQL with its bound values)."""
    blanked = (re.sub(r"'[^']*'|\b\d+\b", "?", sql) for sql in statements)
    return Counter(re.sub(r"\(\?(?:\s*,\s*\?)+\)", "(?)", sql) for sql in blanked)


#-----
# The audit hook is process-wide and cannot be removed (CPython by design), so
# it is installed once, at import, and does nothing unless a trace is active.

_ACTIVE_TRACES: list[SqlTrace] = []
_INIT_PENDING = "Base Connection.__init__ not called"
_ProfileEvent = Literal["call", "return", "c_call", "c_return", "c_exception"]


def _record_statement(trace: SqlTrace) -> Callable[[str], None]:
    return lambda sql: trace.statements.append((threading.current_thread().name, sql))


def _attach_after_init(conn: sqlite3.Connection, trace: SqlTrace) -> None:
    """Arm this thread's profiler to attach the trace once ``__init__`` is done."""
    thread_name = threading.current_thread().name
    previous = sys.getprofile()

    def attach(frame: FrameType, event: _ProfileEvent, arg: object) -> None:
        try:
            conn.set_trace_callback(_record_statement(trace))
        except sqlite3.ProgrammingError as exc:
            if _INIT_PENDING in str(exc):
                return  # still inside __init__: try again at the next event
            trace.untraced.append(thread_name)  # closed before any event
        sys.setprofile(previous)
        if previous is not None:
            previous(frame, event, arg)

    sys.setprofile(attach)


def _trace_new_connections(event: str, args: tuple[Any, ...]) -> None:
    if event != "sqlite3.connect/handle" or not _ACTIVE_TRACES:
        return
    trace = _ACTIVE_TRACES[-1]
    trace.connections.append(threading.current_thread().name)
    _attach_after_init(args[0], trace)


sys.addaudithook(_trace_new_connections)


@contextmanager
def trace_sqlite() -> Iterator[SqlTrace]:
    """Trace every sqlite connection opened inside the block.

    Connections opened BEFORE the block are invisible, so build the app (and
    its persistent connections) inside it.
    """
    trace = SqlTrace()
    _ACTIVE_TRACES.append(trace)
    try:
        yield trace
    finally:
        _ACTIVE_TRACES.remove(trace)


class RowCounter:
    """``row_factory`` that counts rows while still building ``sqlite3.Row``."""

    def __init__(self) -> None:
        self.rows = 0

    def __call__(self, cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> sqlite3.Row:
        self.rows += 1
        return sqlite3.Row(cursor, row)

    def reset(self) -> None:
        self.rows = 0


__all__ = ["RowCounter", "SqlTrace", "statement_shapes", "trace_sqlite"]
