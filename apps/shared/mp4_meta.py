"""In-house MP4 / M4A iTunes metadata (``moov/udta/meta/ilst``) reader / writer (Apache-2.0).

Replaces the GPL ``mutagen.mp4`` for what this project writes into MP4 files:
iTunes text atoms (``©nam`` ...), ``tmpo`` and free-form ``----`` atoms
(``com.apple.iTunes`` mean). Written from the public specifications only
(ISO/IEC 14496-12 box structure; Apple's QuickTime File Format "Metadata"
chapter for ``ilst`` item / ``data`` atoms and well-known data types); no
mutagen source was consulted.

Mini-PRD
--------
✔︎ R1 read every ``ilst`` item; text, integer and free-form values are exposed.
  [if] the file is not MP4 (no ``moov``) [then] Mp4Error, never a guess
  [if] an atom size runs past its parent [then] Mp4Error
✔︎ R2 rewrite ``ilst`` keeping every other atom (and every untouched item) byte
       for byte. The new ``moov`` reuses the old ``moov`` plus any ``free``
       atoms right after it, padding the rest with a ``free`` atom inside
       ``meta``; only when it cannot fit does data after it move, and then every
       ``stco`` / ``co64`` chunk offset pointing past it is moved by the same
       amount so the audio samples stay where the index says they are; in a
       fragmented file every moof tfhd base-data-offset and mfra tfra moof
       offset moves too.
  [if] the new tags fit in the old space [then] no byte outside moov changes
  [if] moov sits before mdat and grows   [then] chunk offsets move and the
       decoded audio is identical
  [if] a fragmented file's moov grows [then] fragment offsets move and the
       decoded audio is identical
  [if] the write fails midway            [then] the original file is intact
"""
from __future__ import annotations

import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from apps.shared.file_rewrite import copy_range, replace_range, rewrite_atomic

ITUNES_MEAN = "com.apple.iTunes"
DATA_UTF8 = 1
DATA_SIGNED_INT = 21
DEFAULT_PADDING = 2048
FREEFORM = b"----"
_PADDING_KINDS = {b"free", b"skip"}
_OFFSET_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"moof", b"traf", b"mfra"}
_FRAGMENT_KINDS = {b"moof", b"mfra"}
_HDLR_MDIR = (
    struct.pack(">I4s", 33, b"hdlr") + bytes(4) + bytes(4) + b"mdir" + b"appl" + bytes(8) + b"\x00"
)


class Mp4Error(Exception):
    """An MP4 file that cannot be read or written faithfully. Never swallowed."""


@dataclass(frozen=True)
class Atom:
    kind: bytes
    start: int  # offset of the size field
    header: int  # 8, or 16 for a 64-bit size
    end: int

    @property
    def payload_start(self) -> int:
        return self.start + self.header


def iter_atoms(buf: bytes, start: int, end: int, where: str) -> Iterator[Atom]:
    """The atoms laid end to end in ``buf[start:end]``."""
    pos = start
    while pos < end:
        if end - pos < 8:
            raise Mp4Error(f"{where}: {end - pos} stray bytes at offset {pos}")
        size, kind = struct.unpack_from(">I4s", buf, pos)
        header = 8
        if size == 1:
            if end - pos < 16:
                raise Mp4Error(f"{where}: truncated 64-bit atom header at offset {pos}")
            (size,) = struct.unpack_from(">Q", buf, pos + 8)
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise Mp4Error(f"{where}: atom {kind!r} at offset {pos} runs past its parent")
        yield Atom(kind, pos, header, pos + size)
        pos += size


def atom(kind: bytes, payload: bytes) -> bytes:
    if len(payload) + 8 > 0xFFFFFFFF:
        raise Mp4Error(f"atom {kind!r} is too large to write")
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def _child(buf: bytes, parent: Atom, kind: bytes, where: str, skip: int = 0) -> Atom | None:
    return next(
        (a for a in iter_atoms(buf, parent.payload_start + skip, parent.end, where) if a.kind == kind), None
    )


# ============================================================== items =======
@dataclass
class Item:
    """One ``ilst`` entry, kept as its raw bytes until it is replaced."""

    raw: bytes

    @property
    def kind(self) -> bytes:
        return self.raw[4:8]

    def _children(self) -> Iterator[tuple[bytes, bytes]]:
        for child in iter_atoms(self.raw, 8, len(self.raw), f"ilst item {self.kind!r}"):
            yield child.kind, self.raw[child.payload_start : child.end]

    def freeform_name(self) -> tuple[str, str] | None:
        """``(mean, name)`` of a ``----`` item, else ``None``."""
        if self.kind != FREEFORM:
            return None
        parts = {kind: payload[4:].decode("utf-8", "replace") for kind, payload in self._children()
                 if kind in (b"mean", b"name")}
        return parts.get(b"mean", ""), parts.get(b"name", "")

    def value(self) -> str | None:
        """The first ``data`` value as text: UTF-8 strings verbatim, integers in decimal."""
        for kind, payload in self._children():
            if kind != b"data" or len(payload) < 8:
                continue
            data_type = int.from_bytes(payload[1:4], "big")
            body = payload[8:]
            if data_type == DATA_UTF8:
                return body.decode("utf-8", "replace")
            if data_type == DATA_SIGNED_INT and len(body) in (1, 2, 4, 8):
                return str(int.from_bytes(body, "big", signed=True))
            return None
        return None


def _data_atom(data_type: int, body: bytes) -> bytes:
    return atom(b"data", struct.pack(">II", data_type, 0) + body)


def _text_item(kind: bytes, value: str) -> Item:
    return Item(atom(kind, _data_atom(DATA_UTF8, value.encode("utf-8"))))


def _freeform_item(mean: str, name: str, value: str) -> Item:
    payload = (
        atom(b"mean", bytes(4) + mean.encode("utf-8"))
        + atom(b"name", bytes(4) + name.encode("utf-8"))
        + _data_atom(DATA_UTF8, value.encode("utf-8"))
    )
    return Item(atom(FREEFORM, payload))


def _fourcc(key: str) -> bytes:
    raw = key.encode("latin-1")
    if len(raw) != 4 or raw == FREEFORM:
        raise Mp4Error(f"invalid ilst item name {key!r}: four Latin-1 characters, not '----'")
    return raw


# ============================================================== model =======
@dataclass
class Mp4Meta:
    path: Path
    moov: Atom
    region_end: int  # end of moov plus the free/skip atoms right after it
    items: list[Item] = field(default_factory=list)

    def text(self, key: str) -> str | None:
        wanted = _fourcc(key)
        return next((i.value() for i in self.items if i.kind == wanted), None)

    def freeform(self, name: str, mean: str = ITUNES_MEAN) -> str | None:
        return next((i.value() for i in self.items if _same_freeform(i, mean, name)), None)

    def set_text(self, key: str, value: str) -> None:
        self._put(lambda i: i.kind == _fourcc(key), _text_item(_fourcc(key), value))

    def set_tempo(self, bpm: int) -> None:
        if not 0 <= bpm <= 0x7FFF:
            raise Mp4Error(f"tmpo must fit a signed 16-bit integer, got {bpm}")
        self._put(lambda i: i.kind == b"tmpo", Item(atom(b"tmpo", _data_atom(DATA_SIGNED_INT, struct.pack(">h", bpm)))))

    def set_freeform(self, name: str, value: str, mean: str = ITUNES_MEAN) -> None:
        """Set a ``----`` item; an existing one with the same mean and a
        case-insensitively equal name is replaced, so no duplicate appears."""
        if not name:
            raise Mp4Error("a free-form item needs a name")
        self._put(lambda i: _same_freeform(i, mean, name), _freeform_item(mean, name, value))

    def _put(self, matches: Callable[[Item], bool], item: Item) -> None:
        index = next((n for n, existing in enumerate(self.items) if matches(existing)), None)
        self.items = [i for i in self.items if not matches(i)]
        self.items.insert(len(self.items) if index is None else index, item)


def _same_freeform(item: Item, mean: str, name: str) -> bool:
    found = item.freeform_name()
    return found is not None and found[0] == mean and found[1].lower() == name.lower()


# ============================================================== read ========
def _top_level(path: Path) -> list[Atom]:
    """Top-level atoms, read from headers only (``mdat`` is never loaded)."""
    atoms: list[Atom] = []
    size_on_disk = path.stat().st_size
    with path.open("rb") as handle:
        if handle.read(8)[4:8] != b"ftyp":
            raise Mp4Error(f"{path}: not an MP4 file (does not start with an ftyp atom)")
        pos = 0
        while pos < size_on_disk:
            handle.seek(pos)
            header = handle.read(16)
            if len(header) < 8:
                raise Mp4Error(f"{path}: {len(header)} stray bytes at offset {pos}")
            size, kind = struct.unpack_from(">I4s", header)
            head = 8
            if size == 1:
                if len(header) < 16:
                    raise Mp4Error(f"{path}: truncated 64-bit atom header at offset {pos}")
                (size,) = struct.unpack_from(">Q", header, 8)
                head = 16
            elif size == 0:
                size = size_on_disk - pos
            if size < head or pos + size > size_on_disk:
                raise Mp4Error(f"{path}: atom {kind!r} at offset {pos} runs past the end of the file")
            atoms.append(Atom(kind, pos, head, pos + size))
            pos += size
    return atoms


def _meta_skip(buf: bytes, meta: Atom) -> int:
    """4 for an ISO full-box ``meta``; 0 for the QuickTime form with no version/flags."""
    return 0 if buf[meta.payload_start + 4 : meta.payload_start + 8] == b"hdlr" else 4


def _load_moov(path: Path, moov: Atom) -> bytes:
    with path.open("rb") as handle:
        handle.seek(moov.start)
        buf = handle.read(moov.end - moov.start)
    if len(buf) != moov.end - moov.start:
        raise Mp4Error(f"{path}: moov runs past the end of the file")
    return buf


def read(path: Path) -> Mp4Meta:
    top = _top_level(path)
    moovs = [a for a in top if a.kind == b"moov"]
    if len(moovs) != 1:
        raise Mp4Error(f"{path}: expected one moov atom, found {len(moovs)}")
    moov_on_disk = moovs[0]
    index = top.index(moov_on_disk)
    region = index + 1
    while region < len(top) and top[region].kind in _PADDING_KINDS:
        region += 1
    buf = _load_moov(path, moov_on_disk)
    moov = Atom(b"moov", 0, moov_on_disk.header, len(buf))
    items: list[Item] = []
    udta = _child(buf, moov, b"udta", str(path))
    meta = _child(buf, udta, b"meta", str(path)) if udta else None
    if meta is not None:
        ilst = _child(buf, meta, b"ilst", str(path), skip=_meta_skip(buf, meta))
        if ilst is not None:
            items = [Item(buf[a.start : a.end]) for a in iter_atoms(buf, ilst.payload_start, ilst.end, str(path))]
    return Mp4Meta(
        path=path,
        moov=moov_on_disk,
        region_end=top[region - 1].end,
        items=items,
    )


# ============================================================== write =======
def _rebuild(
    buf: bytes, parent: Atom, kind: bytes, skip: int, replace_child: Callable[[Atom | None], bytes]
) -> bytes:
    """``parent`` re-emitted with its ``kind`` child replaced by ``replace_child(child)``
    (``child`` is ``None`` when absent: the result is appended)."""
    pre = buf[parent.payload_start : parent.payload_start + skip]
    children = list(iter_atoms(buf, parent.payload_start + skip, parent.end, "moov"))
    target = next((c for c in children if c.kind == kind), None)
    body = [buf[c.start : c.end] if c is not target else replace_child(c) for c in children]
    if target is None:
        body.append(replace_child(None))
    return atom(parent.kind, pre + b"".join(body))


def _new_meta(buf: bytes, meta: Atom | None, ilst: bytes, padding: bytes) -> bytes:
    if meta is None:
        return atom(b"meta", bytes(4) + _HDLR_MDIR + ilst + padding)
    skip = _meta_skip(buf, meta)
    pre = buf[meta.payload_start : meta.payload_start + skip]
    children = list(iter_atoms(buf, meta.payload_start + skip, meta.end, "meta"))
    kept = [buf[c.start : c.end] for c in children if c.kind not in (b"ilst", *_PADDING_KINDS)]
    if not any(c.kind == b"hdlr" for c in children):
        kept.insert(0, _HDLR_MDIR)
    hdlr_at = next((n for n, raw in enumerate(kept) if raw[4:8] == b"hdlr"), -1)
    body = [*kept[: hdlr_at + 1], ilst, padding, *kept[hdlr_at + 1 :]]
    return atom(b"meta", pre + b"".join(body))


def _render_moov(buf: bytes, header: int, items: list[Item], padding_size: int) -> bytes:
    if padding_size and padding_size < 8:
        raise Mp4Error(f"padding of {padding_size} bytes cannot hold a free atom")
    padding = atom(b"free", bytes(padding_size - 8)) if padding_size else b""
    ilst = atom(b"ilst", b"".join(i.raw for i in items))
    moov = Atom(b"moov", 0, header, len(buf))

    def new_udta(udta: Atom | None) -> bytes:
        if udta is None:
            return atom(b"udta", _new_meta(buf, None, ilst, padding))
        return _rebuild(buf, udta, b"meta", 0, lambda found: _new_meta(buf, found, ilst, padding))

    return _rebuild(buf, moov, b"udta", 0, new_udta)


@dataclass(frozen=True)
class _Shift:
    """Moves every absolute file offset at or past ``end`` by ``delta``."""

    start: int  # old moov start
    end: int  # old region end
    delta: int
    where: str

    def moved(self, offset: int, width: int) -> int:
        if self.start <= offset < self.end:
            raise Mp4Error(f"{self.where}: a sample or fragment offset points inside moov")
        if offset < self.end:
            return offset
        offset += self.delta
        if offset >= 1 << (8 * width):
            raise Mp4Error(f"{self.where}: a {8 * width}-bit offset would overflow; nothing was written")
        return offset

    def patch(self, buf: bytearray, parent: Atom, skip: int = 0) -> None:
        """Patch, in place, every offset table under ``parent``: stco / co64 in
        moov, tfhd base-data-offset in moof, tfra moof offsets in mfra."""
        for child in iter_atoms(bytes(buf), parent.payload_start + skip, parent.end, self.where):
            body = child.payload_start
            if child.kind in _OFFSET_CONTAINERS:
                self.patch(buf, child)
            elif child.kind in (b"stco", b"co64"):
                width = 4 if child.kind == b"stco" else 8
                (count,) = struct.unpack_from(">I", buf, body + 4)
                self._patch_run(buf, child, body + 8, count, width, stride=width)
            elif child.kind == b"tfhd" and int.from_bytes(buf[body + 1 : body + 4], "big") & 0x000001:
                self._patch_run(buf, child, body + 8, 1, 8, stride=8)
            elif child.kind == b"tfra":
                version = buf[body]
                width = 8 if version == 1 else 4
                (sizes, count) = struct.unpack_from(">II", buf, body + 8)
                trailer = sum(((sizes >> shift) & 0x3) + 1 for shift in (4, 2, 0))
                self._patch_run(buf, child, body + 16 + width, count, width, stride=2 * width + trailer)

    def _patch_run(
        self, buf: bytearray, table: Atom, first: int, count: int, width: int, *, stride: int
    ) -> None:
        if first + (count - 1) * stride + width > table.end and count:
            raise Mp4Error(f"{self.where}: {table.kind!r} table runs past its atom")
        fmt = ">I" if width == 4 else ">Q"
        for pos in range(first, first + count * stride, stride):
            (offset,) = struct.unpack_from(fmt, buf, pos)
            struct.pack_into(fmt, buf, pos, self.moved(offset, width))


def _patched_atom(raw: bytes, shift: _Shift) -> bytes:
    buf = bytearray(raw)
    top = next(iter_atoms(raw, 0, len(raw), shift.where))
    shift.patch(buf, top)
    return bytes(buf)


def save(path: Path, meta: Mp4Meta) -> None:
    """Rewrite ``path``'s ``ilst`` from ``meta``; samples stay byte-identical."""
    current = read(path)
    if (current.moov, current.region_end) != (meta.moov, meta.region_end):
        raise Mp4Error(f"{path}: changed on disk since it was read")
    buf = _load_moov(path, meta.moov)
    available = meta.region_end - meta.moov.start
    bare = len(_render_moov(buf, meta.moov.header, meta.items, 0))
    room = available - bare
    if room == 0 or room >= 8:
        replace_range(path, meta.moov.start, meta.region_end, _render_moov(buf, meta.moov.header, meta.items, room))
        return
    shift = _Shift(meta.moov.start, meta.region_end, bare + DEFAULT_PADDING - available, str(path))
    new_moov = _render_moov(_patched_atom(buf, shift), meta.moov.header, meta.items, DEFAULT_PADDING)
    top = _top_level(path)
    if any(a.kind == b"sidx" and a.end <= meta.moov.start for a in top):
        raise Mp4Error(f"{path}: a sidx in front of moov would need its references moved; nothing was written")
    tail = [a for a in top if a.start >= meta.region_end]

    def build(src: BinaryIO, out: BinaryIO) -> None:
        copy_range(src, out, 0, meta.moov.start)
        out.write(new_moov)
        for item in tail:
            if item.kind in _FRAGMENT_KINDS:
                src.seek(item.start)
                out.write(_patched_atom(src.read(item.end - item.start), shift))
            else:
                copy_range(src, out, item.start, item.end)

    rewrite_atomic(path, build)


__all__ = [
    "DATA_SIGNED_INT",
    "DATA_UTF8",
    "ITUNES_MEAN",
    "Item",
    "Mp4Error",
    "Mp4Meta",
    "atom",
    "iter_atoms",
    "read",
    "save",
]
