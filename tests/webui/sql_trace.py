"""Count what a request really asks sqlite, on the REAL connections.

Two instruments, neither a mock -- every statement still runs against the
real database file; these only observe:

* :func:`trace_sqlite` wraps ``sqlite3.connect`` so every connection opened
  while it is active gets a ``set_trace_callback`` that records each executed
  statement with the name of the thread that ran it. It also counts the
  connections themselves, which is what #3962 was: one fresh read-only
  connection per listed row.
* :class:`RowCounter` is a ``row_factory`` that counts every row a cursor
  hands back to Python on one connection (the playlist store's), which is
  what #3963 was: every existing member materialized several times per add.

Both expose a ``reset()`` so a test can measure one request in isolation
after fixture setup has run through the same connections.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class SqlTrace:
    """Statements executed and connections opened while tracing is active."""

    #: ``(thread name, expanded SQL)`` per executed statement.
    statements: list[tuple[str, str]] = field(default_factory=list)
    #: Thread name per connection opened.
    connections: list[str] = field(default_factory=list)

    def reset(self) -> None:
        # clear() in place: already-open connections append to THESE lists.
        self.statements.clear()
        self.connections.clear()

    def statements_outside(self, thread_name: str) -> list[str]:
        return [sql for name, sql in self.statements if name != thread_name]

    def connections_outside(self, thread_name: str) -> int:
        return sum(1 for name in self.connections if name != thread_name)


def statement_shapes(statements: list[str]) -> Counter[str]:
    """Statements with literals blanked, so one query over different ids
    counts as one shape (the trace reports SQL with its bound values)."""
    blanked = (re.sub(r"'[^']*'|\b\d+\b", "?", sql) for sql in statements)
    return Counter(re.sub(r"\(\?(?:\s*,\s*\?)+\)", "(?)", sql) for sql in blanked)


@contextmanager
def trace_sqlite(monkeypatch: pytest.MonkeyPatch) -> Iterator[SqlTrace]:
    """Trace every sqlite connection opened inside the block.

    Connections opened BEFORE the block are invisible, so build the app (and
    its persistent connections) inside it.
    """
    real_connect = sqlite3.connect
    trace = SqlTrace()

    def traced_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        trace.connections.append(threading.current_thread().name)
        conn.set_trace_callback(
            lambda sql: trace.statements.append((threading.current_thread().name, sql))
        )
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    yield trace


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
