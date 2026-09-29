"""``python -m apps.open_dj.cli`` -- open-dj reference CLI.

Subcommands (Phase 15 + Phase 15-finisher):

* ``validate``     -- schema validation of an open-dj JSON doc.
* ``canon``        -- rewrite to RFC 8785 canonical form.
* ``diff``         -- structural diff keyed by ``track_id``.
* ``export``       -- vendor DB -> open-dj JSON (rekordbox / djay / serato / traktor).
* ``import``       -- open-dj JSON -> vendor DB (serato / traktor live; rekordbox /
  djay dry-run only -- live writes go through the Phase 4 safety harness
  modules ``apps.sync.apply_ratings`` and ``apps.sync.playlist_apply``).
* ``conformance``  -- run the open-dj conformance corpus (wraps pytest -m conformance).

Safety invariant for ``import``:

* Dry-run is the default. Live writes require BOTH ``--live`` and
  ``--i-understand-the-risks``.
* Serato / Traktor adapters own their safety rails internally
  (``apps.adapters.serato.safety`` + per-adapter ``write()``).
* For rekordbox / djay, Phase 15 did not wire a module-level
  ``import_library`` entry point; the CLI refuses live writes and points
  the user at the cautious writers instead.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.open_dj.canon import to_canonical_bytes
from apps.open_dj.diff import diff_documents, format_report
from apps.open_dj.registry import (
    AdapterNotFoundError,
    available_adapters,
    get_spec,
    load_adapter,
)
from apps.open_dj.validate import validate_document


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return 1
    return handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-dj-tool",
        description="open-dj v0.2 reference CLI.",
    )
    sub = parser.add_subparsers(dest="command")

    p_val = sub.add_parser("validate", help="Validate a doc against the schema.")
    p_val.add_argument("path")
    p_val.set_defaults(handler=_cmd_validate)

    p_can = sub.add_parser("canon", help="Rewrite to canonical JCS form.")
    p_can.add_argument("path", help="Path to .open-dj.json, or '-' for stdin.")
    p_can.add_argument("--in-place", "-i", action="store_true")
    p_can.set_defaults(handler=_cmd_canon)

    p_diff = sub.add_parser("diff", help="Structural diff (track_id-keyed).")
    p_diff.add_argument("a")
    p_diff.add_argument("b")
    p_diff.set_defaults(handler=_cmd_diff)

    adapter_choices = available_adapters()

    p_exp = sub.add_parser(
        "export",
        help="Read a vendor DB and emit an open-dj JSON document.",
    )
    p_exp.add_argument("--adapter", required=True, choices=adapter_choices)
    p_exp.add_argument("--source", required=True, help="Path to vendor DB/folder.")
    p_exp.add_argument("--out", required=True, help="Output .open-dj.json path.")
    p_exp.add_argument(
        "--no-cues",
        action="store_true",
        help="Skip cue-point export (rekordbox / djay module adapters).",
    )
    p_exp.set_defaults(handler=_cmd_export)

    p_imp = sub.add_parser(
        "import",
        help="Read an open-dj JSON document and write it to a vendor DB.",
    )
    p_imp.add_argument("--adapter", required=True, choices=adapter_choices)
    p_imp.add_argument("--source", required=True, help="Path to open-dj JSON.")
    p_imp.add_argument("--target", required=True, help="Vendor DB/folder to write to.")
    p_imp.add_argument(
        "--live",
        action="store_true",
        help="Perform the write (default: dry-run; print plan + exit).",
    )
    p_imp.add_argument(
        "--i-understand-the-risks",
        dest="confirmed",
        action="store_true",
        help="Typed confirm for --live writes (safety rail 3).",
    )
    p_imp.set_defaults(handler=_cmd_import)

    p_conf = sub.add_parser(
        "conformance",
        help="Run the open-dj conformance corpus (pytest -m conformance).",
    )
    p_conf.add_argument(
        "fixture_dir",
        nargs="?",
        default=None,
        help="Optional single fixture dir; omit to run the whole corpus.",
    )
    p_conf.add_argument(
        "--pytest-arg",
        action="append",
        default=[],
        help="Extra args forwarded to pytest (may be repeated).",
    )
    p_conf.set_defaults(handler=_cmd_conformance)

    return parser


# ---------------------------------------------------------------- handlers


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        doc = _load_doc(args.path)
    except _IOError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    errors = validate_document(doc)
    if not errors:
        return 0
    for msg in errors:
        print(msg, file=sys.stderr)
    return 2


def _cmd_canon(args: argparse.Namespace) -> int:
    if args.path == "-":
        raw = sys.stdin.buffer.read()
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"<stdin>: invalid JSON: {exc}", file=sys.stderr)
            return 1
        sys.stdout.buffer.write(to_canonical_bytes(doc))
        return 0

    try:
        doc = _load_doc(args.path)
    except _IOError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    canon_bytes = to_canonical_bytes(doc)

    if args.in_place:
        _atomic_write(Path(args.path), canon_bytes)
    else:
        sys.stdout.buffer.write(canon_bytes)
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    try:
        a = _load_doc(args.a)
        b = _load_doc(args.b)
    except _IOError as exc:
        print(exc.message, file=sys.stderr)
        return 2
    report = diff_documents(a, b)
    if report.is_empty:
        return 0
    print(format_report(report))
    return 1


def _cmd_export(args: argparse.Namespace) -> int:
    """``open-dj-tool export``: vendor DB -> open-dj JSON."""
    try:
        spec = get_spec(args.adapter)
    except AdapterNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not spec.export_supported:
        print(
            f"adapter {spec.name!r} does not support export yet.", file=sys.stderr
        )
        return 1

    source = Path(args.source)
    out = Path(args.out)

    try:
        if spec.family == "module":
            module = load_adapter(spec.name)
            # Module adapters return an ExportResult with a ``.document`` dict.
            result = module.export_library(
                source_path=source,
                out_path=None,
                include_cues=not args.no_cues,
            )
            doc = result.document
        else:  # class family (serato / traktor)
            adapter = load_adapter(spec.name)
            library, _report = adapter.read(source)
            # Route the typed dataclass through the v0.2 wire serializer
            # rather than ``asdict``: the on-disk shape wraps authored
            # fields in ``ProvenanceValue`` envelopes, renames ``cues``
            # to ``cue_points``, collapses ``beats`` into ``beatgrid``,
            # and requires ``schema_version`` + ``kind``. (codex P15)
            from apps.open_dj.wire import library_to_wire_document

            # ``modified_at`` is required by library_to_wire_document (no
            # wall-clock default -- see apps.open_dj.provenance module
            # docstring). The class-family typed Track layer has no
            # per-track source timestamp yet, so "now, once, at the CLI
            # boundary" is the explicit snapshot-time stamp for this run;
            # it is not a hidden default because it is decided here, in
            # the open, rather than guessed inside the serializer.
            doc = library_to_wire_document(
                library, source=spec.name, modified_at=datetime.now(UTC)
            )
    except FileNotFoundError as exc:
        print(f"{args.source}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - surface adapter failures verbatim
        print(f"{spec.name} export failed: {exc}", file=sys.stderr)
        return 2

    data = to_canonical_bytes(doc)
    _atomic_write(out, data)
    track_count = len(doc.get("tracks", []))
    playlist_count = len(doc.get("playlists", []))
    print(
        f"wrote {out} ({track_count} tracks, {playlist_count} playlists)",
        file=sys.stderr,
    )
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    """``open-dj-tool import``: open-dj JSON -> vendor DB.

    Safety:

    * Dry-run by default (``--live`` absent): prints a plan + exits 0.
    * Live requires both ``--live`` and ``--i-understand-the-risks``; if
      either is missing we refuse with exit code 3 so scripts can
      distinguish safety refusal from I/O failure.
    """
    try:
        spec = get_spec(args.adapter)
    except AdapterNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        doc = _load_doc(args.source)
    except _IOError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    errors = validate_document(doc)
    if errors:
        for err in errors:
            print(f"source: {err}", file=sys.stderr)
        return 2

    target = Path(args.target)

    dry_run = not args.live
    if dry_run:
        print(
            f"[dry-run] would import {len(doc.get('tracks', []))} tracks + "
            f"{len(doc.get('playlists', []))} playlists via {spec.name!r} -> {target}",
            file=sys.stderr,
        )
        print(
            "[dry-run] pass --live --i-understand-the-risks to perform the write.",
            file=sys.stderr,
        )
        return 0

    # Live path: require typed confirm AND adapter-level import support.
    if not args.confirmed:
        print(
            "refusing live write: --live requires --i-understand-the-risks",
            file=sys.stderr,
        )
        return 3

    if not spec.import_supported:
        print(
            f"adapter {spec.name!r} does not yet expose a live-import entry point.\n"
            f"  use 'python -m apps.sync.apply_ratings' for ratings or\n"
            f"  'python -m apps.sync.playlist_apply' for playlists (both run\n"
            f"  under the Phase 4 6-rail LiveWriteSession safety harness).",
            file=sys.stderr,
        )
        return 3

    # Class-family (serato / traktor) live write.
    try:
        adapter = load_adapter(spec.name)
        library = _dict_to_library(doc)
        report = adapter.write(library, target)
    except Exception as exc:  # noqa: BLE001 - surface adapter failures verbatim
        print(f"{spec.name} import failed: {exc}", file=sys.stderr)
        return 2

    counts = getattr(report, "counts", {}) or {}
    wrote_tracks = counts.get("tracks_written", 0)
    wrote_playlists = counts.get("playlists_written", 0)
    warning_count = len(getattr(report, "warnings", []) or [])
    print(
        f"wrote {wrote_tracks} tracks, {wrote_playlists} playlists "
        f"via {spec.name!r} -> {target} ({warning_count} warnings)",
        file=sys.stderr,
    )
    return 0


def _cmd_conformance(args: argparse.Namespace) -> int:
    """``open-dj-tool conformance [fixture_dir]``: run the round-trip suite.

    Thin wrapper around ``pytest -m conformance``; when a specific fixture
    directory is named we forward a ``-k`` filter so only its round-trip
    parametrisations run. The default (no arg) runs the entire corpus.
    """
    cmd: list[str] = [sys.executable, "-m", "pytest", "-m", "conformance", "-q"]
    cmd.extend(args.pytest_arg or [])

    if args.fixture_dir is not None:
        fixture_path = Path(args.fixture_dir)
        if not fixture_path.exists():
            print(f"{args.fixture_dir}: no such fixture directory", file=sys.stderr)
            return 1
        # The harness parametrises tests by fixture_id == directory name, so
        # filter with -k against the directory name. Callers can override
        # with --pytest-arg '-k something-else'.
        filter_name = fixture_path.name
        cmd.extend(["-k", filter_name])

    try:
        proc = subprocess.run(cmd, check=False)
    except FileNotFoundError as exc:
        print(f"failed to launch pytest: {exc}", file=sys.stderr)
        return 1
    return proc.returncode


# ------------------------------------------------------------- helpers


class _IOError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _load_doc(path_str: str) -> dict:
    path = Path(path_str)
    if not path.exists():
        raise _IOError(f"{path_str}: no such file")
    try:
        with path.open("rb") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise _IOError(f"{path_str}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise _IOError(f"{path_str}: {exc}") from exc


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _library_to_dict(library: Any) -> dict[str, Any]:
    """Convert a typed ``OpenDjLibrary`` dataclass to the JSON doc shape.

    We call ``asdict`` + strip tuple-ness; the dataclass layer already
    handles ``to_dict`` on ``OpenDjLibrary`` -- prefer that when present.
    """
    if hasattr(library, "to_dict"):
        return library.to_dict()  # type: ignore[no-any-return]
    if is_dataclass(library):
        return asdict(library)
    raise TypeError(f"cannot serialise adapter output: {type(library)!r}")


def _unwrap_prov(value: Any) -> Any:
    """Unwrap an open-dj ``ProvenanceValue`` envelope if present.

    The wire format wraps authored fields (``bpm``, ``rating``, ``key``,
    etc.) in ``{"value": X, "source": ..., "modified_at": ...}``. The typed
    dataclass layer stores the scalar; strip the envelope on ingest so
    adapter ``write()`` paths see the scalar they expect.
    """
    if isinstance(value, dict) and "value" in value and (
        "source" in value or "modified_at" in value
    ):
        return value["value"]
    return value


def _color_hex_to_int(value: Any) -> int | None:
    """Inverse of :func:`apps.open_dj.wire._color_int_to_hex`.

    Accepts ``"#rrggbb"``, ``"rrggbb"``, or a plain int. Returns ``None``
    on anything it cannot parse so loaders degrade gracefully rather than
    crashing on vendor-emitted oddities.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value & 0xFFFFFF
    if isinstance(value, str):
        s = value.strip().lstrip("#")
        if not s:
            return None
        try:
            return int(s, 16) & 0xFFFFFF
        except ValueError:
            return None
    return None


def _cues_from_doc(raw: list[dict[str, Any]], CuePoint) -> tuple:
    """Rehydrate wire-format cue list into typed ``CuePoint`` tuples.

    The v0.2 wire splits a typed ``loop`` cue into a ``loop_in`` /
    ``loop_out`` pair keyed by ``position_ms`` + ``name``; collapse the
    pair back into a single ``loop`` so round-trips through the typed
    layer are lossless. Cue ``color`` is emitted as a ``#rrggbb`` string
    on the wire but stored as an int on the typed layer, so parse here.
    ``index`` is wire-optional (the schema exposes it as a convenience
    for some vendors), so fall back to positional order.
    """
    out: list = []
    skip_next_loop_out_key: set[tuple[int, str]] = set()
    # First pass: collect loop_in -> matching loop_out by (position+length, name)
    # so we can emit a single collapsed loop cue.
    loop_ins: dict[tuple[int, str], dict[str, Any]] = {}
    for c in raw or []:
        if c.get("type") == "loop_in":
            loop_ins[(int(c.get("position_ms", 0)), c.get("name", ""))] = c

    next_index = 0
    for c in raw or []:
        ctype = c.get("type")
        # Collapse loop_in + loop_out -> single "loop"; skip the loop_out.
        if ctype == "loop_out":
            key = (int(c.get("position_ms", 0)), c.get("name", ""))
            if key in skip_next_loop_out_key:
                continue  # handled by its loop_in partner
        length_ms = c.get("length_ms")
        if ctype == "loop_in":
            emit_type = "loop"
            # Look for a partner loop_out to recover length_ms.
            pos_in = int(c.get("position_ms", 0))
            name = c.get("name", "")
            partner_pos = None
            for cc in raw or []:
                if (
                    cc.get("type") == "loop_out"
                    and cc.get("name", "") == name
                    and int(cc.get("position_ms", 0)) >= pos_in
                ):
                    partner_pos = int(cc.get("position_ms", 0))
                    skip_next_loop_out_key.add((partner_pos, name))
                    break
            if length_ms is None and partner_pos is not None:
                length_ms = partner_pos - pos_in
        else:
            emit_type = ctype
        idx = c.get("index")
        if idx is None:
            idx = next_index
        next_index = int(idx) + 1
        out.append(
            CuePoint(
                index=int(idx),
                position_ms=int(c.get("position_ms", 0)),
                type=emit_type,
                name=c.get("name", ""),
                color_rgb=_color_hex_to_int(c.get("color") or c.get("color_rgb")),
                length_ms=int(length_ms) if length_ms is not None else None,
            )
        )
    return tuple(out)


def _beats_from_doc(raw: Any, BeatGridPoint) -> tuple:
    """Rehydrate a v0.2 ``beatgrid`` object into ``BeatGridPoint`` tuple.

    Wire shape: ``{"origin_ms": float, "bpm": float, "algorithm": str,
    "beats": [position_ms, ...], "source": ProvenanceValue}``. We treat
    the first anchor's BPM as the locked BPM for all constant grids;
    for variable grids the wire does not (yet) carry per-anchor BPMs, so
    we fall back to the header BPM across the board. This is lossless
    for the constant case (overwhelmingly common) and conservative for
    variable grids. The last anchor is marked ``terminal`` so writers
    that need a sentinel (e.g. Serato) can emit one.
    """
    if not raw:
        return ()
    if isinstance(raw, list):
        # Permit a bare list of positions; rare, but some callers emit it.
        beats_list = raw
        header_bpm = 0.0
    else:
        beats_list = raw.get("beats") or []
        header_bpm = float(raw.get("bpm") or 0.0)
    out: list = []
    n = len(beats_list)
    for i, pos in enumerate(beats_list):
        out.append(
            BeatGridPoint(
                position_ms=int(pos),
                bpm=header_bpm,
                terminal=(i == n - 1),
            )
        )
    return tuple(out)


def _dict_to_library(doc: dict[str, Any]):
    """Inverse of :func:`_library_to_dict` -- JSON doc -> ``OpenDjLibrary``.

    Minimal loader; mirrors the private helper in
    ``tests/test_conformance.py`` so we do not introduce a hard test
    dependency from the CLI. Provenance-wrapped scalars (``bpm``,
    ``rating``, ``key``) are unwrapped here because the typed layer
    stores raw scalars.

    This loader is the inverse of :mod:`apps.open_dj.wire`, so it
    accepts both v0.1-style docs (``cues``, plain ``track_ids``) and the
    v0.2 wire (``cue_points`` with hex ``color``, ``beatgrid`` object,
    ``tracks_ordered`` playlist entries). Dropping any of these on the
    way in would silently lose user data on the next write, so the
    lossy-on-import bug (Codex finding P15-F2) is fixed by plumbing
    ``beatgrid``, ``tracks_ordered``, and cue ``color`` through to the
    typed model.
    """
    from apps.open_dj import BeatGridPoint, CuePoint, OpenDjLibrary, Playlist, Track

    tracks = tuple(
        Track(
            track_id=t["track_id"],
            file_path=t.get("file_path", ""),
            title=t.get("title", ""),
            artists=tuple(t.get("artists", ())),
            album=t.get("album", ""),
            bpm=_unwrap_prov(t.get("bpm")),
            key_camelot=_unwrap_prov(t.get("key") or t.get("key_camelot")),
            rating=_unwrap_prov(t.get("rating")),
            duration_ms=t.get("duration_ms"),
            # ``x_play_count`` / ``x_color_rgb`` are the wire-side
            # passthroughs for typed-only scalars; accept either form so
            # docs produced by either side of the boundary round-trip.
            play_count=t.get("play_count", t.get("x_play_count", 0)) or 0,
            color_rgb=t.get("color_rgb", t.get("x_color_rgb")),
            cues=_cues_from_doc(
                list(t.get("cue_points") or t.get("cues") or []),
                CuePoint,
            ),
            beats=_beats_from_doc(t.get("beatgrid"), BeatGridPoint),
            isrc=t.get("isrc"),
            extensions=t.get("extensions", {}) or {},
        )
        for t in doc.get("tracks", [])
    )
    playlists = tuple(
        Playlist(
            name=p["name"],
            # v0.2 wire uses ``tracks_ordered``; v0.1 docs and the
            # in-memory round-trip form use ``track_ids``. Accept both
            # so an imported v0.2 JSON actually keeps its playlists.
            track_ids=tuple(p.get("tracks_ordered") or p.get("track_ids", ())),
        )
        for p in doc.get("playlists", [])
    )
    return OpenDjLibrary(
        version=doc.get("schema_version") or doc.get("version", "0.1"),
        tracks=tracks,
        playlists=playlists,
        extensions=doc.get("extensions", {}) or {},
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
