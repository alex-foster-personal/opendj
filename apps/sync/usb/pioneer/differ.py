"""Structural diff for Pioneer USB exports (``exportLibrary.db`` focus).

This module is the typed successor of ``scripts/scratch/diff_usb_exports.py``.
That script grew organically while we chased the writer-vs-real-export
divergence for CAT-06; the logic is sound but the outputs were ad-hoc
markdown strings, which is awkward to consume from pytest or from a
matrix runner over N fixtures.

Here we split the work into three layers:

1. :func:`snapshot_onelibrary` — open a ``exportLibrary.db`` via the
   ``rbox`` Rust binding against a safe tempfile copy (never mutate the
   input) and capture the bits that matter for structural comparison:
   per-table row counts + playlist name list.

2. :func:`diff_snapshots` — compare two snapshots under one of two
   modes:

   * ``identity`` — writer passthrough, we expect *every* table row
     count and *every* playlist name to match bit-for-bit.
   * ``overlay``  — writer + a known set of appended playlists; we
     expect the per-playlist-row delta to equal
     ``len(expected_playlist_additions)`` and no other changes.

   The verdict is explicit: ``structurally_identical``,
   ``expected_overlay_delta``, or ``unexpected_divergence``.

3. :func:`round_trip_via_writer` — given a fixture's OneLibrary path,
   run it through :func:`writer_rbox.write_onelibrary` (with or without
   an overlay) and return the path to the writer's output, so callers
   can snapshot + diff without duplicating the fixture-safety dance.

Plus :func:`diff_pair_cli` — the original ad-hoc markdown report that
the scratch tool used to emit. Kept working so existing docs
(``docs/rb-usb-export-writer-diff-report.md``) can be regenerated
byte-identically; the scratch CLI now delegates to this function.

Why keep both shapes?
---------------------

The typed shape drives the :mod:`apps.sync.usb.pioneer.__main__`
``diff-matrix`` subcommand and the pytest harness. The markdown shape
is still useful for pair-wise ad-hoc investigations where a human wants
to eyeball file-presence and ANLZ-tag diffs too — those details aren't
part of the matrix (they're pair-specific), so we keep that code here
and expose it unchanged via :func:`diff_pair_cli`.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import shutil
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "OneLibraryTableStats",
    "OneLibrarySchemaSnapshot",
    "OneLibraryDiff",
    "snapshot_onelibrary",
    "diff_snapshots",
    "round_trip_via_writer",
    "build_report",
    "diff_pair_cli",
]


# ---------------------------------------------------------------------------
# Typed snapshot / diff surface (new).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OneLibraryTableStats:
    """Per-table stats from a OneLibrary snapshot.

    ``distinct_ids`` is ``None`` when the underlying rbox accessor does
    not expose a stable integer primary key (we don't enforce it today,
    but the field is reserved for future drift checks).
    """

    table_name: str
    row_count: int
    distinct_ids: int | None = None


@dataclass(frozen=True)
class OneLibrarySchemaSnapshot:
    """Cheap, structural fingerprint of a ``exportLibrary.db`` file.

    Byte-level details (size, head, sha256) are *not* part of this
    snapshot: rbox/SQLCipher rewrite the page MACs on every open, so
    any writer-touched DB is byte-different even when structurally
    equivalent. We compare what's stable across writes.
    """

    path: Path
    tables: tuple[OneLibraryTableStats, ...]
    playlist_names: tuple[str, ...]
    error: str | None = None

    def table_rows(self) -> dict[str, int]:
        return {t.table_name: t.row_count for t in self.tables}


_Mode = Literal["identity", "overlay"]
_Verdict = Literal[
    "structurally_identical",
    "expected_overlay_delta",
    "unexpected_divergence",
]


@dataclass(frozen=True)
class OneLibraryDiff:
    """Result of comparing two :class:`OneLibrarySchemaSnapshot`\\ s."""

    left: OneLibrarySchemaSnapshot
    right: OneLibrarySchemaSnapshot
    mode: _Mode
    table_deltas: dict[str, int] = field(default_factory=dict)
    missing_tables: frozenset[str] = field(default_factory=frozenset)
    playlist_additions: frozenset[str] = field(default_factory=frozenset)
    playlist_removals: frozenset[str] = field(default_factory=frozenset)
    verdict: _Verdict = "unexpected_divergence"
    reason: str = ""


# ---------------------------------------------------------------------------
# Snapshot implementation — read-only against a tempfile copy.
# ---------------------------------------------------------------------------


def _head_hex(path: Path, n: int = 32) -> str:
    with path.open("rb") as fh:
        return fh.read(n).hex()


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _relative_files(root: Path) -> set[Path]:
    return {
        p.relative_to(root)
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith("._")
    }


def snapshot_onelibrary(path: Path) -> OneLibrarySchemaSnapshot:
    """Open a OneLibrary file via rbox and extract row counts + playlists.

    IMPORTANT: ``rbox.OneLibrary(path)`` opens the DB read-write and may
    checkpoint the WAL (folding pending pages into the main file) on
    close. Any fixture we point it at would therefore change byte
    content on every call, which breaks fixture-safety. To avoid that
    we always copy the DB (plus any ``-wal`` / ``-shm`` sidecars) into
    a throwaway temp directory and snapshot *that* copy.

    Returns an :class:`OneLibrarySchemaSnapshot`; on any failure, the
    ``error`` field is populated and ``tables`` / ``playlist_names``
    are empty, so callers can still render a partial report.
    """
    try:
        from rbox import OneLibrary  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        return OneLibrarySchemaSnapshot(
            path=path,
            tables=(),
            playlist_names=(),
            error=f"rbox not importable: {exc}",
        )

    with tempfile.TemporaryDirectory(prefix="onelib-snap-") as tdir:
        tmp_db = Path(tdir) / path.name
        shutil.copyfile(path, tmp_db)
        for suffix in ("-wal", "-shm"):
            src_side = path.with_name(path.name + suffix)
            if src_side.is_file():
                shutil.copyfile(
                    src_side, tmp_db.with_name(tmp_db.name + suffix)
                )

        try:
            db = OneLibrary(str(tmp_db))
        except Exception as exc:  # noqa: BLE001
            return OneLibrarySchemaSnapshot(
                path=path,
                tables=(),
                playlist_names=(),
                error=f"rbox.OneLibrary open failed: {exc}",
            )

        playlist_names: list[str] = []
        row_counts: dict[str, int] = {}

        # Playlists — treated as a first-class table for the matrix.
        try:
            playlists = list(db.get_playlists())
            playlist_names = [str(p["name"]) for p in playlists]
            row_counts["playlist"] = len(playlist_names)
        except Exception as exc:  # noqa: BLE001
            # Record the problem, but keep scanning the other accessors
            # so we still return something useful.
            return OneLibrarySchemaSnapshot(
                path=path,
                tables=(),
                playlist_names=(),
                error=f"get_playlists failed: {exc}",
            )

        for attr, tbl in (
            ("get_contents", "content"),
            ("get_artists", "artist"),
            ("get_albums", "album"),
            ("get_genres", "genre"),
            ("get_keys", "key"),
            ("get_labels", "label"),
            ("get_colors", "color"),
        ):
            fn = getattr(db, attr, None)
            if fn is None:
                continue
            try:
                row_counts[tbl] = sum(1 for _ in fn())
            except Exception:  # noqa: BLE001
                # Best-effort — skip tables that rbox refuses on this DB.
                continue

        tables = tuple(
            OneLibraryTableStats(table_name=name, row_count=count)
            for name, count in sorted(row_counts.items())
        )
        return OneLibrarySchemaSnapshot(
            path=path,
            tables=tables,
            playlist_names=tuple(playlist_names),
        )


# ---------------------------------------------------------------------------
# Diff.
# ---------------------------------------------------------------------------


def diff_snapshots(
    left: OneLibrarySchemaSnapshot,
    right: OneLibrarySchemaSnapshot,
    *,
    mode: _Mode,
    expected_playlist_additions: Iterable[str] = (),
) -> OneLibraryDiff:
    """Compare two snapshots under the given ``mode``.

    ``identity`` mode: right must have the same set of tables, same row
    count per table, and the same playlist names as left.

    ``overlay`` mode: right must equal left *plus*
    ``expected_playlist_additions`` new playlist names, and the
    ``playlist`` row count must grow by exactly that count; every
    other table row count must be unchanged. No playlists should be
    removed.
    """
    if mode not in ("identity", "overlay"):
        raise ValueError(f"mode must be 'identity' or 'overlay', got {mode!r}")

    left_rows = left.table_rows()
    right_rows = right.table_rows()
    all_tables = set(left_rows) | set(right_rows)
    missing = frozenset(
        t for t in all_tables
        if (t in left_rows) ^ (t in right_rows)
    )
    table_deltas = {
        t: right_rows.get(t, 0) - left_rows.get(t, 0) for t in sorted(all_tables)
    }

    left_pl = set(left.playlist_names)
    right_pl = set(right.playlist_names)
    additions = frozenset(right_pl - left_pl)
    removals = frozenset(left_pl - right_pl)

    if left.error or right.error:
        return OneLibraryDiff(
            left=left,
            right=right,
            mode=mode,
            table_deltas=table_deltas,
            missing_tables=missing,
            playlist_additions=additions,
            playlist_removals=removals,
            verdict="unexpected_divergence",
            reason=f"snapshot error: left={left.error!r} right={right.error!r}",
        )

    expected_set = frozenset(expected_playlist_additions)

    if mode == "identity":
        if missing:
            reason = f"table set differs: {sorted(missing)}"
            return OneLibraryDiff(
                left, right, mode, table_deltas, missing,
                additions, removals, "unexpected_divergence", reason,
            )
        nonzero = {t: d for t, d in table_deltas.items() if d != 0}
        if nonzero:
            reason = f"row count deltas: {nonzero}"
            return OneLibraryDiff(
                left, right, mode, table_deltas, missing,
                additions, removals, "unexpected_divergence", reason,
            )
        if additions or removals:
            reason = (
                f"playlist set differs: +{sorted(additions)} "
                f"-{sorted(removals)}"
            )
            return OneLibraryDiff(
                left, right, mode, table_deltas, missing,
                additions, removals, "unexpected_divergence", reason,
            )
        return OneLibraryDiff(
            left, right, mode, table_deltas, missing,
            additions, removals, "structurally_identical",
            "structurally identical",
        )

    # overlay mode.
    if missing:
        reason = f"table set differs under overlay: {sorted(missing)}"
        return OneLibraryDiff(
            left, right, mode, table_deltas, missing,
            additions, removals, "unexpected_divergence", reason,
        )
    if removals:
        reason = f"overlay must not remove playlists: {sorted(removals)}"
        return OneLibraryDiff(
            left, right, mode, table_deltas, missing,
            additions, removals, "unexpected_divergence", reason,
        )
    # Every non-playlist table must be unchanged.
    bad_deltas = {
        t: d for t, d in table_deltas.items() if t != "playlist" and d != 0
    }
    if bad_deltas:
        reason = (
            f"non-playlist tables changed under overlay: {bad_deltas}"
        )
        return OneLibraryDiff(
            left, right, mode, table_deltas, missing,
            additions, removals, "unexpected_divergence", reason,
        )
    expected_delta = len(expected_set)
    actual_delta = table_deltas.get("playlist", 0)
    if actual_delta != expected_delta:
        reason = (
            f"playlist delta {actual_delta:+d} != expected {expected_delta:+d}"
        )
        return OneLibraryDiff(
            left, right, mode, table_deltas, missing,
            additions, removals, "unexpected_divergence", reason,
        )
    if expected_set and additions != expected_set:
        reason = (
            f"playlist additions {sorted(additions)} != expected "
            f"{sorted(expected_set)}"
        )
        return OneLibraryDiff(
            left, right, mode, table_deltas, missing,
            additions, removals, "unexpected_divergence", reason,
        )

    return OneLibraryDiff(
        left, right, mode, table_deltas, missing,
        additions, removals, "expected_overlay_delta",
        f"overlay added {actual_delta} playlist(s) as expected",
    )


# ---------------------------------------------------------------------------
# Round-trip through the writer.
# ---------------------------------------------------------------------------


def round_trip_via_writer(
    fixture_pioneer: Path,
    *,
    workdir: Path,
    overlay_playlists: Iterable[tuple[str, Iterable[int]]] = (),
) -> Path:
    """Copy a fixture's ``exportLibrary.db`` through the writer.

    ``fixture_pioneer`` must point at a ``PIONEER/`` directory (the
    reader-facing root of a USB export). We read
    ``<PIONEER>/rekordbox/exportLibrary.db`` via the writer
    (passing it through :func:`writer_rbox.write_onelibrary` with the
    given overlay playlists) and return the path to the writer's
    output file.

    ``workdir`` must be a scratch directory (typically ``tmp_path`` in
    a pytest). The writer copies the template into ``workdir`` before
    touching it, so the fixture itself is never mutated — the
    writer's own fixture-safety guard enforces this as belt-and-
    braces.
    """
    # Lazy import so callers without rbox installed can still import
    # this module (e.g. to read the typed dataclasses).
    from .writer_rbox import (  # type: ignore[import-not-found]
        OneLibraryWriteError,
        PlaylistSpec,
        write_onelibrary,
    )

    template_src = fixture_pioneer / "rekordbox" / "exportLibrary.db"
    if not template_src.is_file():
        raise FileNotFoundError(
            f"Expected OneLibrary template at {template_src}"
        )

    # Copy the fixture DB (and any WAL/SHM sidecars) into workdir first,
    # so the writer's fixture-safety guard sees a non-fixture path when
    # we hand it the template. (The guard rejects paths that contain
    # 'tests/fixtures/'.)
    workdir.mkdir(parents=True, exist_ok=True)
    template_copy = workdir / "template-exportLibrary.db"
    shutil.copyfile(template_src, template_copy)
    for suffix in ("-wal", "-shm"):
        src_side = template_src.with_name(template_src.name + suffix)
        if src_side.is_file():
            shutil.copyfile(
                src_side, template_copy.with_name(template_copy.name + suffix)
            )

    # If the fixture ships a WAL (the big one does: 4 MB of pending
    # pages), we must fold it into the main file before handing the
    # path to ``write_onelibrary``. The writer internally only does
    # ``shutil.copyfile(template, output)`` which copies the main DB
    # without the WAL — so any pending pages would be silently lost
    # and the writer's output would be missing dozens of rows (=76 in
    # our big fixture). Opening + dropping an rbox handle against the
    # copy forces SQLite's normal WAL-checkpoint on close, which
    # rewrites the main file to contain everything.
    try:
        from rbox import OneLibrary as _OL  # type: ignore[import-not-found]

        _h = _OL(str(template_copy))
        del _h
    except Exception:  # noqa: BLE001
        # If rbox isn't importable we'll fail later with a clearer
        # error from write_onelibrary; don't mask that here.
        pass

    output = workdir / "writer-exportLibrary.db"
    specs = [
        PlaylistSpec(name=name, track_ids=tuple(int(i) for i in ids))
        for name, ids in overlay_playlists
    ]
    try:
        write_onelibrary(
            template_path=template_copy,
            output_path=output,
            playlists=specs,
            overwrite=True,
        )
    except OneLibraryWriteError as exc:
        raise RuntimeError(
            f"writer failed on fixture {fixture_pioneer}: {exc}"
        ) from exc
    return output


# ---------------------------------------------------------------------------
# Legacy ad-hoc markdown report (preserved for docs/diff reports).
# ---------------------------------------------------------------------------


def _fmt_set_delta(
    name: str, only_a: Iterable[Path], only_b: Iterable[Path], limit: int = 20
) -> list[str]:
    out = [f"### {name}", ""]
    only_a_l = sorted(str(p) for p in only_a)
    only_b_l = sorted(str(p) for p in only_b)
    out.append(f"* Only in A: **{len(only_a_l)}**")
    if only_a_l:
        for p in only_a_l[:limit]:
            out.append(f"  * `{p}`")
        if len(only_a_l) > limit:
            out.append(f"  * … (+{len(only_a_l) - limit} more)")
    out.append(f"* Only in B: **{len(only_b_l)}**")
    if only_b_l:
        for p in only_b_l[:limit]:
            out.append(f"  * `{p}`")
        if len(only_b_l) > limit:
            out.append(f"  * … (+{len(only_b_l) - limit} more)")
    out.append("")
    return out


def _diff_onelibrary_markdown(
    a: OneLibrarySchemaSnapshot, b: OneLibrarySchemaSnapshot,
    a_size: int, a_head: str, b_size: int, b_head: str,
) -> list[str]:
    bytes_eq = a_size == b_size and a_head == b_head
    out = [
        "### exportLibrary.db (OneLibrary, SQLCipher)",
        "",
        f"* A size: **{a_size:,}** bytes; head32: `{a_head}`",
        f"* B size: **{b_size:,}** bytes; head32: `{b_head}`",
        f"* Header-bytes match: **{'yes' if bytes_eq else 'no'}** "
        f"(SQLCipher rewrites header + re-salts page MACs on every write, "
        f"so byte-level inequality is expected for any DB touched by "
        f"rbox)",
        "",
    ]
    if a.error:
        out.append(f"* ⚠️ A could not be fully opened via rbox: {a.error}")
    if b.error:
        out.append(f"* ⚠️ B could not be fully opened via rbox: {b.error}")
    if a.error or b.error:
        out.append("")

    a_rows = a.table_rows()
    b_rows = b.table_rows()
    if a_rows or b_rows:
        all_tables = sorted(set(a_rows) | set(b_rows))
        out.append("#### Row counts")
        out.append("")
        out.append("| Table | A rows | B rows | Δ |")
        out.append("|---|---:|---:|---:|")
        for t in all_tables:
            ra = a_rows.get(t)
            rb = b_rows.get(t)
            delta = (
                f"{rb - ra:+d}" if ra is not None and rb is not None
                else "—"
            )
            out.append(
                f"| `{t}` | {ra if ra is not None else '—'} | "
                f"{rb if rb is not None else '—'} | {delta} |"
            )
        out.append("")

    if a.playlist_names or b.playlist_names:
        set_a = set(a.playlist_names)
        set_b = set(b.playlist_names)
        out.append(f"* Playlists: A={len(set_a)}, B={len(set_b)}")
        extra = sorted(set_b - set_a)
        gone = sorted(set_a - set_b)
        if extra:
            out.append(f"* Added in B: `{extra[:20]}`"
                       + (" …" if len(extra) > 20 else ""))
        if gone:
            out.append(f"* Missing in B: `{gone[:20]}`"
                       + (" …" if len(gone) > 20 else ""))
        if not extra and not gone:
            out.append("* Playlist sets are identical.")
        out.append("")

    return out


def _diff_pdb(a_root: Path, b_root: Path) -> list[str]:
    out = ["### export.pdb / exportExt.pdb (plain PDB)", ""]
    out.append("| File | A size | A head32 | B size | B head32 | Bytes eq? |")
    out.append("|---|---:|---|---:|---|---|")
    for name in ("rekordbox/export.pdb", "rekordbox/exportExt.pdb"):
        pa = a_root / name
        pb = b_root / name
        if not pa.exists() or not pb.exists():
            out.append(
                f"| `{name}` | "
                f"{'(missing)' if not pa.exists() else f'{pa.stat().st_size:,}'} | — | "
                f"{'(missing)' if not pb.exists() else f'{pb.stat().st_size:,}'} | — | n/a |"
            )
            continue
        sa, sb = pa.stat().st_size, pb.stat().st_size
        ha, hb = _head_hex(pa), _head_hex(pb)
        eq = "✅" if sa == sb and _sha256(pa) == _sha256(pb) else "❌"
        out.append(
            f"| `{name}` | {sa:,} | `{ha}` | {sb:,} | `{hb}` | {eq} |"
        )
    out.append("")
    return out


def _diff_anlz(a_root: Path, b_root: Path, n: int = 5) -> list[str]:
    out = ["### ANLZ tag presence (sampled)", ""]
    a_anlz = a_root / "USBANLZ"
    b_anlz = b_root / "USBANLZ"
    if not a_anlz.is_dir() or not b_anlz.is_dir():
        out.append(f"* Skipped — USBANLZ missing in A ({a_anlz.is_dir()}) "
                   f"or B ({b_anlz.is_dir()}).")
        out.append("")
        return out

    # Local import so a missing reader module doesn't break the whole
    # differ package at import time.
    from .reader import read_anlz_dir  # type: ignore[import-not-found]

    a_dirs = {p.relative_to(a_anlz) for p in a_anlz.rglob("ANLZ0000.DAT")}
    b_dirs = {p.relative_to(b_anlz) for p in b_anlz.rglob("ANLZ0000.DAT")}
    common = sorted(a_dirs & b_dirs)[:n]
    if not common:
        out.append("* No common ANLZ0000.DAT files found in both trees.")
        out.append("")
        return out

    out.append("| ANLZ dir | A tags | B tags | Symmetric Δ |")
    out.append("|---|---|---|---|")
    for rel in common:
        ad = (a_anlz / rel).parent
        bd = (b_anlz / rel).parent
        try:
            asum = read_anlz_dir(ad)
            bsum = read_anlz_dir(bd)
        except Exception as exc:  # noqa: BLE001
            out.append(f"| `{rel}` | err | err | `{exc!s}` |")
            continue
        atags = set(asum.get("tag_counts", {}).keys())
        btags = set(bsum.get("tag_counts", {}).keys())
        delta = sorted(atags.symmetric_difference(btags))
        out.append(
            f"| `{rel}` | `{sorted(atags)}` | `{sorted(btags)}` | "
            f"`{delta or 'none'}` |"
        )
    out.append("")
    return out


def build_report(a_root: Path, b_root: Path, title: str) -> str:
    """Legacy pair-wise markdown report (file-presence + ANLZ + OneLibrary).

    Equivalent to the old ``scripts.scratch.diff_usb_exports`` output;
    reproduced here so the scratch CLI can delegate.
    """
    a_root = a_root.resolve()
    b_root = b_root.resolve()
    lines = [f"# {title}", ""]
    lines.append(f"* **A** (real export):   `{a_root}`")
    lines.append(f"* **B** (writer output): `{b_root}`")
    lines.append("")

    files_a = _relative_files(a_root)
    files_b = _relative_files(b_root)
    lines.append("## File presence")
    lines.append("")
    lines.append(f"* A files (real): **{len(files_a):,}**")
    lines.append(f"* B files (writer): **{len(files_b):,}**")
    lines.append("")
    lines.extend(_fmt_set_delta("Presence delta", files_a - files_b, files_b - files_a))

    lines.append("## Byte-level comparison")
    lines.append("")
    lines.extend(_diff_pdb(a_root, b_root))

    a_one = a_root / "rekordbox" / "exportLibrary.db"
    b_one = b_root / "rekordbox" / "exportLibrary.db"
    if a_one.exists() and b_one.exists():
        snap_a = snapshot_onelibrary(a_one)
        snap_b = snapshot_onelibrary(b_one)
        lines.extend(_diff_onelibrary_markdown(
            snap_a, snap_b,
            a_one.stat().st_size, _head_hex(a_one),
            b_one.stat().st_size, _head_hex(b_one),
        ))
    else:
        lines.append("### exportLibrary.db")
        lines.append("")
        lines.append(
            f"* A present: {a_one.exists()} ({a_one}); "
            f"B present: {b_one.exists()} ({b_one})"
        )
        lines.append("")

    lines.append("## ANLZ sample")
    lines.append("")
    lines.extend(_diff_anlz(a_root, b_root))

    return "\n".join(lines) + "\n"


def diff_pair_cli(argv: list[str] | None = None) -> int:
    """Thin CLI: the old ``diff_usb_exports`` entrypoint.

    Preserved so ``scripts.scratch.diff_usb_exports`` keeps working for
    humans regenerating pair-wise reports (e.g. the existing
    ``docs/rb-usb-export-writer-diff-report.md``).
    """
    ap = argparse.ArgumentParser(
        description="Compare two Pioneer USB export trees (pair-wise)."
    )
    ap.add_argument("--a", required=True, type=Path, help="Path to PIONEER tree A")
    ap.add_argument("--b", required=True, type=Path, help="Path to PIONEER tree B")
    ap.add_argument("--out", type=Path, help="Output markdown file (default: stdout)")
    ap.add_argument("--title", default="USB export diff", help="Report title")
    args = ap.parse_args(argv)

    report = build_report(args.a, args.b, args.title)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"[i] Wrote {args.out}")
    else:
        sys.stdout.write(report)
    return 0


# ---------------------------------------------------------------------------
# Matrix runner (consumed by ``python -m apps.sync.usb.pioneer diff-matrix``).
# ---------------------------------------------------------------------------


def discover_fixtures(glob_pattern: str = "rb-usb-export*") -> list[str]:
    """Return a sorted list of candidate fixture names matching ``glob_pattern``.

    Scans ``tests/fixtures/`` for:

    * directories whose name matches the glob (without a ``.extern``
      suffix), and
    * ``<name>.extern`` marker files (the returned name has the suffix
      stripped).

    The output is a stable sorted list of logical names. Resolution to
    a real path is deferred — callers should pass each name through
    :func:`tests.fixtures._resolver.fixture_path`.

    This function does not touch the file contents — discovery is
    cheap and safe to run during pytest collection.
    """
    # Import lazily so this module stays usable outside the repo.
    from tests.fixtures._resolver import FIXTURES_ROOT  # type: ignore[import-not-found]

    names: set[str] = set()
    for entry in sorted(FIXTURES_ROOT.iterdir()):
        name = entry.name
        if entry.is_dir() and entry.match(glob_pattern) and not name.endswith(".extern"):
            names.add(name)
        elif entry.is_file() and name.endswith(".extern"):
            stem = name[: -len(".extern")]
            # match the stem against the glob.
            if Path(stem).match(glob_pattern):
                names.add(stem)
    return sorted(names)


@dataclass(frozen=True)
class MatrixRow:
    """One row of the diff-matrix output.

    ``status`` is ``"ok"`` when the snapshot/diff ran, ``"skipped"``
    when the fixture host is unmounted, and ``"error"`` when anything
    else went wrong (e.g. missing ``exportLibrary.db``).
    """

    fixture: str
    mode: str
    status: str  # "ok" | "skipped" | "error"
    verdict: str  # "structurally_identical" | "expected_overlay_delta" | ...
    tracks: int | None
    playlists: int | None
    delta_content: int | None
    delta_playlist: int | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "mode": self.mode,
            "status": self.status,
            "verdict": self.verdict,
            "tracks": self.tracks,
            "playlists": self.playlists,
            "delta_content": self.delta_content,
            "delta_playlist": self.delta_playlist,
            "reason": self.reason,
        }


_VERDICT_ICON = {
    "structurally_identical": "✅ identical",
    "expected_overlay_delta": "✅ expected",
    "unexpected_divergence": "❌ divergent",
    "skipped": "⏭ skipped",
    "error": "⚠️ error",
}

# The only rb-usb-export* fixture that is genuinely optional (LaCie-hosted,
# huge, dev-machine-only). Every other discovered name is REQUIRED CAT-06
# acceptance coverage and must fail closed (an "error" row, not "skipped")
# rather than let an unmounted host silently pass the CLI's exit code.
# Shared with tests/sync/usb/test_diff_matrix.py so the pytest matrix and
# the `diff-matrix` CLI can't drift on which fixtures are optional.
OPTIONAL_FIXTURE_NAMES = frozenset({"rb-usb-export-big"})


def _overlay_spec_for_fixture(pioneer: Path, workdir: Path) -> list[tuple[str, list[int]]]:
    """Pick up to 3 content IDs from the fixture for the overlay test.

    Returns ``[]`` when the fixture has zero tracks (caller treats
    empty overlay spec as "identity" effectively and will skip).
    """
    try:
        from rbox import OneLibrary  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return []
    # Read IDs from a safe copy of the DB so we don't mutate the fixture.
    db_src = pioneer / "rekordbox" / "exportLibrary.db"
    if not db_src.is_file():
        return []
    workdir.mkdir(parents=True, exist_ok=True)
    tmp = workdir / "pick-ids.db"
    shutil.copyfile(db_src, tmp)
    for suffix in ("-wal", "-shm"):
        side = db_src.with_name(db_src.name + suffix)
        if side.is_file():
            shutil.copyfile(side, tmp.with_name(tmp.name + suffix))
    try:
        db = OneLibrary(str(tmp))
    except Exception:  # noqa: BLE001
        return []
    ids: list[int] = []
    try:
        for c in db.get_contents():
            try:
                ids.append(int(c["id"]))
            except Exception:  # noqa: BLE001
                continue
            if len(ids) >= 3:
                break
    except Exception:  # noqa: BLE001
        ids = []
    finally:
        del db
    if not ids:
        return []
    return [("diff-matrix-test-playlist", ids)]


def _resolve_matrix_fixture(name: str, *, optional: bool) -> tuple[Path | None, MatrixRow | None]:
    """Resolve one matrix fixture, or the "skipped"/"error" row to record instead.

    Returns ``(root, None)`` on success, or ``(None, row)`` when the
    caller should append ``row`` and move on to the next fixture.

    Only ``optional`` (:data:`OPTIONAL_FIXTURE_NAMES`) fixtures may report
    "skipped" for an unmounted host. Every other name is REQUIRED CAT-06
    acceptance coverage, so an unavailable host or a fixture that fails its
    content-contract check becomes an "error" row instead (which
    ``_cmd_diff_matrix`` maps to a nonzero exit code) -- fail closed, per
    AGENTS.md, rather than let the CLI succeed without ever having
    exercised the fixture. Mirrors
    ``tests/sync/usb/test_diff_matrix.py``'s ``_resolve_or_skip()`` so the
    pytest matrix and this CLI-facing implementation cannot silently
    diverge on which fixtures a missing host is allowed to hide.
    """
    from tests.fixtures._resolver import (  # type: ignore[import-not-found]
        FixtureContractMismatch,
        FixtureNotAvailable,
        fixture_path,
        verify_fixture_contract,
    )

    def _row(status: str, reason: str) -> MatrixRow:
        return MatrixRow(
            fixture=name, mode="-", status=status, verdict=status,
            tracks=None, playlists=None, delta_content=None, delta_playlist=None,
            reason=reason,
        )

    try:
        root = fixture_path(name)
    except FixtureNotAvailable as exc:
        return None, _row("skipped" if optional else "error", str(exc))
    except FileNotFoundError as exc:
        return None, _row("error", str(exc))

    if not optional:
        try:
            verify_fixture_contract(name, root)
        except FixtureContractMismatch as exc:
            return None, _row("error", str(exc))

    return root, None


def run_matrix(
    *,
    fixture_glob: str = "rb-usb-export*",
    work_root: Path | None = None,
    include_overlay: bool = True,
) -> list[MatrixRow]:
    """Run identity + overlay diff for every discovered fixture.

    ``work_root`` is used as a parent for per-fixture scratch dirs. If
    ``None``, a :func:`tempfile.mkdtemp` is used.

    See :func:`_resolve_matrix_fixture` for the fail-closed fixture
    resolution rule applied to each discovered name.
    """
    rows: list[MatrixRow] = []
    names = discover_fixtures(fixture_glob)
    work_root = work_root or Path(tempfile.mkdtemp(prefix="diff-matrix-"))

    for name in names:
        root, error_row = _resolve_matrix_fixture(name, optional=name in OPTIONAL_FIXTURE_NAMES)
        if error_row is not None:
            rows.append(error_row)
            continue
        assert root is not None

        pioneer = root / "PIONEER"
        if not pioneer.is_dir():
            rows.append(MatrixRow(
                fixture=name, mode="—", status="error",
                verdict="error", tracks=None, playlists=None,
                delta_content=None, delta_playlist=None,
                reason=f"no PIONEER/ under {root}",
            ))
            continue

        db_src = pioneer / "rekordbox" / "exportLibrary.db"
        if not db_src.is_file():
            rows.append(MatrixRow(
                fixture=name, mode="—", status="error",
                verdict="error", tracks=None, playlists=None,
                delta_content=None, delta_playlist=None,
                reason=f"no exportLibrary.db under {pioneer}",
            ))
            continue

        fixture_work = work_root / name
        fixture_work.mkdir(parents=True, exist_ok=True)
        left = snapshot_onelibrary(db_src)

        # identity
        try:
            out = round_trip_via_writer(
                pioneer, workdir=fixture_work / "identity",
            )
            right = snapshot_onelibrary(out)
            diff = diff_snapshots(left, right, mode="identity")
            rows.append(MatrixRow(
                fixture=name, mode="identity", status="ok",
                verdict=diff.verdict,
                tracks=left.table_rows().get("content"),
                playlists=left.table_rows().get("playlist"),
                delta_content=diff.table_deltas.get("content", 0),
                delta_playlist=diff.table_deltas.get("playlist", 0),
                reason=diff.reason,
            ))
        except Exception as exc:  # noqa: BLE001
            rows.append(MatrixRow(
                fixture=name, mode="identity", status="error",
                verdict="error",
                tracks=left.table_rows().get("content"),
                playlists=left.table_rows().get("playlist"),
                delta_content=None, delta_playlist=None,
                reason=f"identity failed: {exc}",
            ))

        # overlay
        if include_overlay:
            overlay = _overlay_spec_for_fixture(
                pioneer, fixture_work / "pick"
            )
            if not overlay:
                rows.append(MatrixRow(
                    fixture=name, mode="overlay", status="skipped",
                    verdict="skipped",
                    tracks=left.table_rows().get("content"),
                    playlists=left.table_rows().get("playlist"),
                    delta_content=None, delta_playlist=None,
                    reason="fixture has zero content rows; no overlay possible",
                ))
            else:
                try:
                    out = round_trip_via_writer(
                        pioneer, workdir=fixture_work / "overlay",
                        overlay_playlists=overlay,
                    )
                    right = snapshot_onelibrary(out)
                    expected = {name_ for name_, _ in overlay}
                    diff = diff_snapshots(
                        left, right,
                        mode="overlay",
                        expected_playlist_additions=expected,
                    )
                    rows.append(MatrixRow(
                        fixture=name, mode="overlay", status="ok",
                        verdict=diff.verdict,
                        tracks=left.table_rows().get("content"),
                        playlists=left.table_rows().get("playlist"),
                        delta_content=diff.table_deltas.get("content", 0),
                        delta_playlist=diff.table_deltas.get("playlist", 0),
                        reason=diff.reason,
                    ))
                except Exception as exc:  # noqa: BLE001
                    rows.append(MatrixRow(
                        fixture=name, mode="overlay", status="error",
                        verdict="error",
                        tracks=left.table_rows().get("content"),
                        playlists=left.table_rows().get("playlist"),
                        delta_content=None, delta_playlist=None,
                        reason=f"overlay failed: {exc}",
                    ))

    return rows


def render_matrix_markdown(rows: list[MatrixRow], *, now: _dt.datetime | None = None) -> str:
    # Emit RFC3339-ish UTC without the bare 'Z' suffix (the timezone
    # is already carried as '+00:00' thanks to the tz-aware now()).
    when = (
        (now or _dt.datetime.now(_dt.UTC))
        .replace(microsecond=0)
        .isoformat()
    )
    out = [
        "# USB Export Diff Matrix",
        "",
        f"Generated: {when}",
        "",
        "| Fixture | Mode | Tracks | Playlists | Δ content | Δ playlist | Verdict |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        def _cell(v: Any) -> str:
            if v is None:
                return "—"
            if isinstance(v, int):
                return f"{v:+d}" if v != 0 else "0"
            return str(v)

        verdict_label = _VERDICT_ICON.get(r.verdict, r.verdict)
        if r.status == "skipped":
            verdict_label = f"⏭ {r.reason}"[:80]
        elif r.status == "error":
            verdict_label = f"⚠️ {r.reason}"[:80]
        out.append(
            f"| `{r.fixture}` | {r.mode} | "
            f"{r.tracks if r.tracks is not None else '—'} | "
            f"{r.playlists if r.playlists is not None else '—'} | "
            f"{_cell(r.delta_content)} | "
            f"{_cell(r.delta_playlist)} | "
            f"{verdict_label} |"
        )
    out.append("")
    return "\n".join(out) + "\n"


def render_matrix_json(rows: list[MatrixRow]) -> str:
    return json.dumps(
        {"rows": [r.to_dict() for r in rows]},
        indent=2, sort_keys=False,
    ) + "\n"
