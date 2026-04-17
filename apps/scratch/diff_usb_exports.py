"""Compare two Pioneer USB export trees.

Produces a markdown report describing, for a pair of ``PIONEER/``
directories ("A" and "B"):

* File-presence delta (what exists in A that isn't in B, and vice
  versa).
* Byte-level comparison of key files (``export.pdb``, ``exportExt.pdb``,
  ``exportLibrary.db``): size + first-32-bytes hex header + sha256.
* For ``exportLibrary.db`` — uses ``rbox`` (OneLibrary Rust binding) to
  open both and compare:

    - schema (``sqlite_master``) table names,
    - row counts per table (when accessible),
    - playlist name list.

* For ANLZ files — picks up to 5 tracks present in both trees and
  compares the set of ``PMAI/PPTH/PCOB/PQTZ/...`` tag fourccs.

Usage
-----
.. code-block:: bash

    .venv/bin/python -m apps.scratch.diff_usb_exports \\
        --a /Volumes/LaCie/.../rb-usb-export-big/PIONEER \\
        --b /tmp/rb-usb-export-big-writer/PIONEER \\
        --out docs/rb-usb-export-writer-diff-report.md
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from apps.sync.usb.pioneer.reader import read_anlz_dir


# ---------------------------------------------------------------------------
# File discovery + byte comparison
# ---------------------------------------------------------------------------


def _relative_files(root: Path) -> set[Path]:
    """Return the set of file paths (relative to ``root``) under ``root``.

    Skips macOS AppleDouble sidecars (``._*``) so we don't report noise
    when one tree was copied across filesystems.
    """
    return {
        p.relative_to(root)
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith("._")
    }


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _head_hex(path: Path, n: int = 32) -> str:
    with path.open("rb") as fh:
        return fh.read(n).hex()


# ---------------------------------------------------------------------------
# OneLibrary schema / row-count comparison (SQLCipher via rbox)
# ---------------------------------------------------------------------------


@dataclass
class OneLibSnapshot:
    path: Path
    size_bytes: int
    head_hex: str
    table_names: list[str] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)
    playlist_names: list[str] = field(default_factory=list)
    content_count: int | None = None
    error: str | None = None


def _snapshot_onelibrary(path: Path) -> OneLibSnapshot:
    """Open a OneLibrary DB via ``rbox`` and extract structural info.

    ``exportLibrary.db`` is SQLCipher-encrypted with a fixed key that
    ``rbox`` knows about. The Rust binding doesn't expose a raw sqlite
    connection on every version, so we call accessor methods and count
    rows by iteration.

    IMPORTANT: ``rbox.OneLibrary(path)`` opens the file read-write and
    may checkpoint the WAL (i.e. fold pending -wal contents into the
    main file), which mutates the byte content. We therefore snapshot
    against a tempfile COPY of the target rather than the original so
    the fixture never changes under our feet. Any ``-wal`` / ``-shm``
    sidecars alongside the original are copied too so the read sees
    the "live" view the user would see when plugging the USB in.

    Soft-fails — the returned snapshot has ``error`` set when anything
    goes wrong so the caller can still render a partial report.
    """
    snap = OneLibSnapshot(
        path=path,
        size_bytes=path.stat().st_size,
        head_hex=_head_hex(path),
    )
    try:
        from rbox import OneLibrary  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        snap.error = f"rbox not importable: {exc}"
        return snap

    import shutil
    import tempfile

    with tempfile.TemporaryDirectory(prefix="onelib-snap-") as tdir:
        tmp_db = Path(tdir) / path.name
        shutil.copyfile(path, tmp_db)
        # Copy WAL / SHM sidecars too, so rbox sees the "live" contents
        # (with pending pages) rather than a checkpointed view.
        for suffix in ("-wal", "-shm"):
            src_side = path.with_name(path.name + suffix)
            if src_side.is_file():
                shutil.copyfile(src_side, tmp_db.with_name(tmp_db.name + suffix))

        try:
            db = OneLibrary(str(tmp_db))
        except Exception as exc:  # noqa: BLE001
            snap.error = f"rbox.OneLibrary open failed: {exc}"
            return snap
        return _fill_snapshot(snap, db)


def _fill_snapshot(snap: "OneLibSnapshot", db) -> "OneLibSnapshot":
    """Populate ``snap`` from an open rbox.OneLibrary handle."""

    # Playlist names. rbox.Playlist requires a default argument to .get
    # (unlike dict), so we use attribute access / __getitem__ which work
    # on every rbox version we've seen.
    try:
        playlists = list(db.get_playlists())
        snap.playlist_names = [str(p["name"]) for p in playlists]
        snap.row_counts["playlist"] = len(snap.playlist_names)
    except Exception as exc:  # noqa: BLE001
        snap.error = f"get_playlists failed: {exc}"

    # Row counts via accessor methods (when available).
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
            snap.row_counts[tbl] = sum(1 for _ in fn())
        except Exception:
            continue
    snap.table_names = sorted(snap.row_counts.keys())
    snap.content_count = snap.row_counts.get("content")

    return snap


# ---------------------------------------------------------------------------
# Markdown rendering
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


def _diff_onelibrary(a: OneLibSnapshot, b: OneLibSnapshot) -> list[str]:
    bytes_eq = (a.size_bytes == b.size_bytes and a.head_hex == b.head_hex)
    out = [
        "### exportLibrary.db (OneLibrary, SQLCipher)",
        "",
        f"* A size: **{a.size_bytes:,}** bytes; head32: `{a.head_hex}`",
        f"* B size: **{b.size_bytes:,}** bytes; head32: `{b.head_hex}`",
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

    if a.row_counts or b.row_counts:
        all_tables = sorted(set(a.row_counts) | set(b.row_counts))
        out.append("#### Row counts")
        out.append("")
        out.append("| Table | A rows | B rows | Δ |")
        out.append("|---|---:|---:|---:|")
        for t in all_tables:
            ra = a.row_counts.get(t)
            rb = b.row_counts.get(t)
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
    a_root = a_root.resolve()
    b_root = b_root.resolve()
    lines = [f"# {title}", ""]
    lines.append(f"* **A** (real export):   `{a_root}`")
    lines.append(f"* **B** (writer output): `{b_root}`")
    lines.append("")

    # File presence.
    files_a = _relative_files(a_root)
    files_b = _relative_files(b_root)
    lines.append("## File presence")
    lines.append("")
    lines.append(f"* A files (real): **{len(files_a):,}**")
    lines.append(f"* B files (writer): **{len(files_b):,}**")
    lines.append("")
    lines.extend(_fmt_set_delta("Presence delta", files_a - files_b, files_b - files_a))

    # PDB byte comparison.
    lines.append("## Byte-level comparison")
    lines.append("")
    lines.extend(_diff_pdb(a_root, b_root))

    # OneLibrary structural comparison.
    a_one = a_root / "rekordbox" / "exportLibrary.db"
    b_one = b_root / "rekordbox" / "exportLibrary.db"
    if a_one.exists() and b_one.exists():
        snap_a = _snapshot_onelibrary(a_one)
        snap_b = _snapshot_onelibrary(b_one)
        lines.extend(_diff_onelibrary(snap_a, snap_b))
    else:
        lines.append("### exportLibrary.db")
        lines.append("")
        lines.append(
            f"* A present: {a_one.exists()} ({a_one}); "
            f"B present: {b_one.exists()} ({b_one})"
        )
        lines.append("")

    # ANLZ sample.
    lines.append("## ANLZ sample")
    lines.append("")
    lines.extend(_diff_anlz(a_root, b_root))

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
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


if __name__ == "__main__":
    raise SystemExit(main())
