"""Is this ``tracks`` row an identity loser? Answered per row (LIBM-120 L6).

:class:`apps.sync_hub.sync_set.HeldKeys` used to answer it by electing the
whole library (:func:`apps.sync_hub.engine_identity_map.effective_identity_remap`)
when a walk started. A hub pull page is one walk, so a first sync of 10,000
tracks re-read and re-elected the library once per page: about 4 s, and
quadratic in the library.

A row's verdict depends only on its COMPONENT: the rows reachable from it
through shared identity keys, plus the persisted hub remaps. Every group it
sits in, every champion those groups elect and every remap chain from it stay
inside that set, so electing the component alone gives the same answer as
electing the library. :class:`IdentityLoserVerdicts` finds the component with
index seeks and caches the verdict of every row in it.

Exactness does not rest on stored keys being canonical. Election keys are
``str(value).strip()`` of a hash and a normalized ISRC, and an index seek
matches raw values, so:

* a hash key ``K`` is sought as the prefix range ``[K, K + U+10FFFF)``, which
  holds every raw value that strips to ``K`` by its end;
* a raw hash that starts with anything outside printable ASCII (leading
  whitespace, a number, a BLOB) sorts below ``'!'`` or above ``'~'``, and
  those rows are read once per walk and join any component whose keys they
  share. On canonical data both ranges are empty;
* ISRC-only rows (no hash at all) normalize away separators an index cannot
  see, so the first ISRC-only row asked about reads every live row that has an
  ISRC and could have no hash, once per walk. Such rows are rare after the
  ``audio_hash`` backfill; a library made mostly of them pays one scan per walk.

``tests/cloudsync/test_identity_verdicts.py`` pins the verdicts equal to the
library-wide election over randomized libraries.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from apps.sync_hub.engine_identity_map import PersistedRemap, effective_identity_remap
from apps.sync_hub.protocol_common import table_columns
from apps.sync_hub.sync_set import (
    IdentityRow,
    identity_keys,
    identity_remap_from_rows,
    identity_row_select,
)

# ----- config -------------------------------------------------------------------


class CFG:
    #: Upper bound of a prefix range: the highest code point, so ``K + CFG.PREFIX_END``
    #: sorts above every value that starts with ``K``.
    PREFIX_END: str = "\U0010ffff"
    #: Printable ASCII bounds. A raw hash starting outside them may strip or
    #: stringify to a key its raw bytes do not start with.
    PRINTABLE_LOW: str = "!"
    PRINTABLE_HIGH: str = "~"
    #: Verdicts one walk decides row by row before it elects the whole library
    #: once instead. Four pull pages' worth (``DEFAULT_PULL_LIMIT`` is 500), so a
    #: page never reaches it and a full walk (offer, digest) pays one scan.
    ELECT_LIBRARY_AFTER_VERDICTS: int = 2_000


# ----- verdicts -----------------------------------------------------------------


class IdentityLoserVerdicts:
    """Per-row identity-loser verdicts for one walk over one connection.

    Same lifetime rule as :class:`apps.sync_hub.sync_set.HeldKeys`: one walk,
    then discard, because the cache does not see later writes.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._verdicts: dict[str, bool] = {}
        self._persisted: PersistedRemap | None = None
        self._odd_hash_rows: list[tuple[int, IdentityRow]] | None = None
        self._library_losers: frozenset[str] | None = None
        self._select: str | None = None
        self._hash_columns: tuple[str, ...] | None = None

    def is_loser(self, stable_id: str) -> bool:
        if self._library_losers is not None:
            return stable_id in self._library_losers
        if stable_id in self._verdicts:
            return self._verdicts[stable_id]
        if len(self._verdicts) >= CFG.ELECT_LIBRARY_AFTER_VERDICTS:
            self._library_losers = frozenset(effective_identity_remap(self._conn))
            return stable_id in self._library_losers
        self._decide_component_of(stable_id)
        return self._verdicts[stable_id]

    def expect(self, count: int) -> None:
        """A caller is about to ask ``count`` more verdicts.

        When that would cross the cutover anyway, elect the library now
        instead of deciding the first rows one component at a time: a
        10,042-member bundle asks for every one of its tracks at once.
        """
        if (
            self._library_losers is None
            and len(self._verdicts) + count >= CFG.ELECT_LIBRARY_AFTER_VERDICTS
        ):
            self._library_losers = frozenset(effective_identity_remap(self._conn))

    # ----- component --------------------------------------------------------

    def _decide_component_of(self, stable_id: str) -> None:
        seed = self._rows(" AND stable_id = ?", (stable_id,))
        if not seed:
            self._record([], {}, also=stable_id)
            return
        keys = identity_keys(seed[0][1], seed[0][2], seed[0][3])
        if keys.hashes:
            component = self._hash_component(seed[0])
        elif keys.isrc:
            component = self._isrc_only_rows()
        else:
            component = seed
        self._record(component, identity_remap_from_rows(component), also=stable_id)

    def _record(
        self, component: list[IdentityRow], local: dict[str, str], *, also: str
    ) -> None:
        decided = {str(row[0]) for row in component} | {also}
        losers = self._persisted_remap().effective_losers(local, decided)
        for pk in decided:
            self._verdicts[pk] = pk in losers

    def _hash_component(self, seed: IdentityRow) -> list[IdentityRow]:
        """Every live row reachable from ``seed`` through shared hash keys, in rowid order."""
        found: dict[str, tuple[int, IdentityRow]] = {}
        pending = [seed]
        searched: set[str] = set()
        while pending:
            row = pending.pop()
            for key in identity_keys(row[1], row[2], row[3]).hashes:
                if key in searched:
                    continue
                searched.add(key)
                for rowid, partner in self._rows_with_hash(key):
                    if str(partner[0]) not in found:
                        found[str(partner[0])] = (rowid, partner)
                        pending.append(partner)
        return [row for _, row in sorted(found.values(), key=lambda item: item[0])]

    def _rows_with_hash(self, key: str) -> Iterable[tuple[int, IdentityRow]]:
        """Rows whose stripped ``content_hash`` or ``audio_hash`` is ``key``."""
        candidates = list(self._odd_rows())
        for column in self._hash_column_names():
            candidates.extend(
                self._rows_with_rowid(
                    f" AND {column} >= ? AND {column} < ?", (key, key + CFG.PREFIX_END)
                )
            )
        return [
            (rowid, row)
            for rowid, row in candidates
            if key in identity_keys(row[1], row[2], row[3]).hashes
        ]

    def _odd_rows(self) -> list[tuple[int, IdentityRow]]:
        """Rows with a hash no prefix range can find: read once per walk."""
        if self._odd_hash_rows is None:
            bounds = [
                (f" AND {column} {op} ?", (edge,))
                for column in self._hash_column_names()
                for op, edge in (("<", CFG.PRINTABLE_LOW), (">", CFG.PRINTABLE_HIGH))
            ]
            rows = {
                rowid: row
                for where, params in bounds
                for rowid, row in self._rows_with_rowid(where, params)
            }
            self._odd_hash_rows = [(rowid, rows[rowid]) for rowid in sorted(rows)]
        return self._odd_hash_rows

    def _isrc_only_rows(self) -> list[IdentityRow]:
        """Every live row whose only identity key is a normalizable ISRC.

        The SQL filter is a superset (a hash with no alphanumeric character
        may still strip to empty); :func:`identity_keys` decides.
        """
        no_hash = " AND ".join(
            f"({column} IS NULL OR typeof({column}) != 'text' "
            f"OR {column} NOT GLOB '*[0-9A-Za-z]*')"
            for column in self._hash_column_names()
        )
        rows = self._rows_with_rowid(f" AND isrc IS NOT NULL AND {no_hash}", ())
        return [
            row
            for _, row in rows
            if (keys := identity_keys(row[1], row[2], row[3])).isrc and not keys.hashes
        ]

    # ----- reads ------------------------------------------------------------

    def _persisted_remap(self) -> PersistedRemap:
        if self._persisted is None:
            self._persisted = PersistedRemap.load(self._conn)
        return self._persisted

    def _hash_column_names(self) -> tuple[str, ...]:
        if self._hash_columns is None:
            columns = table_columns(self._conn, "tracks")
            self._hash_columns = tuple(
                column for column in ("content_hash", "audio_hash") if column in columns
            )
        return self._hash_columns

    def _rows(self, where: str, params: tuple[object, ...]) -> list[IdentityRow]:
        return [row for _, row in self._rows_with_rowid(where, params)]

    def _rows_with_rowid(
        self, where: str, params: tuple[object, ...]
    ) -> list[tuple[int, IdentityRow]]:
        if self._select is None:
            self._select = identity_row_select(self._conn).replace(
                "SELECT ", "SELECT rowid, ", 1
            )
        return [
            (int(row[0]), tuple(row[1:]))
            for row in self._conn.execute(f"{self._select}{where}", params)
        ]


__all__ = ["CFG", "IdentityLoserVerdicts"]
