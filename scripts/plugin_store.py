"""Build, pack and sign plugin store artifacts (PLUG-6522, PLUG-6522b).

    python -m scripts.plugin_store keygen --key-id odj-store-2026 --out store-key.pem
    python -m scripts.plugin_store hash  <plugin-dir>             # rewrite manifest "files"
    python -m scripts.plugin_store pack  <plugin-dir> --out dist/  # -> <id>-<version>.zip
    python -m scripts.plugin_store index --channel stable --key store-key.pem --key-id ID \
        --base-url https://.../archives --listings listings/  dist/*.zip --out index/

``keygen`` prints the PUBLIC key line to paste into
``apps/plugins/signing.py:TRUSTED_STORE_KEYS``; the private PEM belongs in the
secret store, never in git. ``index`` writes ``<channel>.json`` and
``<channel>.json.sig`` exactly as :func:`apps.plugins.catalog.parse_index`
reads them, and re-reads its own output through that parser before exiting,
so an index this tool writes is one the app accepts.

LISTINGS (PLUG-6522k). Every plugin in an index needs a store listing:
``<listings>/<plugin id>/listing.json`` holds the problem-first text
(``apps/plugins/listing.py``) and names each screenshot by file::

    "screenshots": [{"file": "dark.png", "alt": "...", "theme": "dark"}]

``index`` hashes and measures each image, copies it to
``<out>/media/<plugin id>/<sha256>.<ext>`` and writes the pinned entry, so
the screenshots are published next to the index they are signed in.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
)

from apps.plugins.catalog import INDEX_SCHEMA, MAX_INDEX_BYTES, parse_index
from apps.plugins.installer import MAX_ARCHIVE_BYTES, InstallError, check_archive, sha256_hex
from apps.plugins.listing import MAX_SCREENSHOT_BYTES, MEDIA_TYPES, ListingError, image_size, sniff_media_type
from apps.plugins.manifest import MANIFEST_FILENAME, ManifestError, check_archive_path, parse_manifest
from apps.plugins.signing import public_key_b64, sign
from apps.plugins.state import write_atomic
from apps.shared import private_files

#: Never packed: build litter that would otherwise change hashes between machines.
_SKIP_NAMES = frozenset({"__pycache__", ".DS_Store"})


def _refuse_symlink(root: Path, rel: str) -> None:
    """Refuse a payload path that is, or sits under, a symlink.

    Following one would publish bytes from outside the plugin as signed
    payload; the installer refuses symlinks too.
    """
    if root.is_symlink():  # every path below would be read from the link's target
        raise SystemExit(f"{root} is a symlink; point the tool at the real folder")
    path = root
    for part in Path(rel).parts:
        path = path / part
        if path.is_symlink():
            raise SystemExit(
                f"{rel}: {path.relative_to(root).as_posix()} is a symlink; plugins ship regular files only"
            )


def _payload_files(root: Path) -> list[str]:
    out = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        _refuse_symlink(root, rel)
        if path.is_dir() or rel == MANIFEST_FILENAME:
            continue
        if any(part in _SKIP_NAMES for part in path.relative_to(root).parts):
            continue
        if path.suffix == ".pyc":
            continue
        out.append(rel)
    return out


def serialize_manifest(raw: dict[str, Any]) -> bytes:
    """The one serialization ``hash`` writes: indented JSON, LF line ends, UTF-8."""
    return (json.dumps(raw, indent=2) + "\n").encode("utf-8")


def cmd_hash(root: Path) -> int:
    _refuse_symlink(root, MANIFEST_FILENAME)  # it is rewritten: never through a link
    manifest_path = root / MANIFEST_FILENAME
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["files"] = {rel: sha256_hex((root / rel).read_bytes()) for rel in _payload_files(root)}
    # Bytes, not text: no platform newline translation, so the same archive hash everywhere.
    candidate = serialize_manifest(raw)
    parse_manifest(candidate)  # the app must accept it; a refusal leaves the source manifest untouched
    write_atomic(manifest_path, candidate)
    print(f"hashed {len(raw['files'])} files into {manifest_path}")
    return 0


def cmd_pack(root: Path, out_dir: Path) -> int:
    _refuse_symlink(root, MANIFEST_FILENAME)
    manifest_bytes = (root / MANIFEST_FILENAME).read_bytes()
    manifest = parse_manifest(manifest_bytes)
    for rel in manifest.files:
        _refuse_symlink(root, rel)  # before any archive exists, so a refusal leaves none
    payload: list[tuple[str, bytes]] = []
    for rel, digest in sorted(manifest.files.items()):
        data = (root / rel).read_bytes()
        if sha256_hex(data) != digest:  # checked before the archive is opened, so a refusal leaves none
            print(f"{rel} does not match its manifest hash; run `hash` first", file=sys.stderr)
            return 1
        payload.append((rel, data))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{manifest.plugin_id}-{manifest.version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_FILENAME, manifest_bytes)
        for rel, data in payload:
            zf.writestr(rel, data)
    print(f"packed {target} ({target.stat().st_size} bytes)")
    return 0


def _load_key(path: Path) -> Ed25519PrivateKey:
    key = load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{path} is not an Ed25519 private key")
    return key


def _write_private(out: Path, data: bytes) -> None:
    """Create ``out`` readable and writable by this account only, before any key byte lands.

    The file is created empty, restricted through the owner-only seam
    (POSIX mode 0600, or a Windows ACL granting this account alone), and
    read back; only then is the key written. A restriction that cannot be
    applied or verified removes the empty file and refuses.
    """
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        try:
            private_files.restrict_to_owner(out)
            private = private_files.is_owner_only(out)
        except private_files.PrivateFileError as exc:
            private, reason = False, str(exc)
        else:
            reason = "its permissions still let other accounts read it"
        if not private:
            handle.close()
            out.unlink()
            raise SystemExit(f"could not make {out} private ({reason}); no key was written")
        handle.write(data)


def cmd_keygen(key_id: str, out: Path) -> int:
    if out.exists():
        raise SystemExit(f"{out} exists; refusing to overwrite a key")
    key = Ed25519PrivateKey.generate()
    _write_private(out, key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    print(f"private key written to {out}; move it into the secret store")
    print(f'TRUSTED_STORE_KEYS entry:  "{key_id}": "{public_key_b64(key)}",')
    return 0


def _listing_file(listing_dir: Path, rel: str) -> bytes:
    """A screenshot's bytes, only from a regular file inside the listing's own folder.

    The bytes are published under the signed index, so a path that is
    absolute, climbs out, or passes through a symlink is refused, as for
    plugin payload files.
    """
    try:
        check_archive_path(rel)
    except ManifestError as exc:
        raise SystemExit(f"screenshot {rel!r}: {exc}") from exc
    _refuse_symlink(listing_dir, rel)
    path = listing_dir / rel
    if not path.is_file():
        raise SystemExit(f"screenshot {rel!r} is not a file in {listing_dir}")
    with path.open("rb") as handle:
        data = handle.read(MAX_SCREENSHOT_BYTES + 1)  # never more than the limit, however large the file
    if len(data) > MAX_SCREENSHOT_BYTES:
        raise SystemExit(f"screenshot {rel!r} is over {MAX_SCREENSHOT_BYTES} bytes, so every client would refuse it")
    return data


def _decoded_size(name: str, data: bytes) -> tuple[int, int]:
    """Decode the whole image, as a browser will, and return its size.

    The header and chunk checks in ``image_size`` cannot prove a bitstream
    renders (a WebP can be well formed and hold no image at all), and the
    signed index publishes these bytes to every client.
    """
    from PIL import Image  # the packaging tool only; the app never decodes screenshots

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return image.size
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise SystemExit(f"{name}: it does not decode as an image ({exc}), so no browser could show it") from exc


def _listing(listings: Path, plugin_id: str, out_dir: Path) -> dict:
    """The listing for the index, with each screenshot measured, pinned and copied."""
    source = listings / plugin_id / "listing.json"
    _refuse_symlink(listings, f"{plugin_id}/listing.json")
    if not source.exists():
        raise SystemExit(f"{plugin_id} has no store listing at {source}")
    raw = json.loads(source.read_text(encoding="utf-8"))
    shots = []
    for shot in raw.get("screenshots", []):
        data = _listing_file(source.parent, shot["file"])
        ext = Path(shot["file"]).suffix.lstrip(".").lower()
        if ext not in MEDIA_TYPES:
            raise SystemExit(f"{shot['file']}: screenshots are .png or .webp")
        try:
            width, height = image_size(data)
        except ListingError as exc:
            raise SystemExit(f"{shot['file']}: {exc}") from exc
        if sniff_media_type(data) != MEDIA_TYPES[ext]:  # clients check the bytes against the extension
            raise SystemExit(f"{shot['file']}: its bytes are not {MEDIA_TYPES[ext]}, so rename it")
        if _decoded_size(shot["file"], data) != (width, height):
            raise SystemExit(f"{shot['file']}: its header size does not match the decoded image")
        digest = sha256_hex(data)
        path = f"media/{plugin_id}/{digest}.{ext}"
        (out_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (out_dir / path).write_bytes(data)
        shots.append(
            {
                "path": path,
                "sha256": digest,
                "size": len(data),
                "width": width,
                "height": height,
                "alt": shot["alt"],
                "theme": shot["theme"],
            }
        )
    return {**raw, "screenshots": shots}


def cmd_index(
    channel: str,
    archives: list[Path],
    key: Path,
    key_id: str,
    base_url: str,
    out_dir: Path,
    blocklist: Path | None,
    listings: Path,
) -> int:
    checked = []
    for archive in archives:  # all of them before anything is written or signed, one held at a time
        size = archive.stat().st_size
        if size > MAX_ARCHIVE_BYTES:  # never read whole: clients stop downloading at the cap
            raise SystemExit(
                f"{archive}: every client would refuse it (too-large): it is {size} bytes; "
                f"clients download at most {MAX_ARCHIVE_BYTES}"
            )
        data = archive.read_bytes()
        try:
            checked.append((archive, check_archive(data), hashlib.sha256(data).hexdigest(), len(data)))
        except InstallError as exc:
            raise SystemExit(f"{archive}: every client would refuse it ({exc.code}): {exc}") from exc
        del data
    rows = []
    for archive, manifest, digest, size in checked:
        rows.append(
            {
                "manifest": manifest.to_wire(),
                "archive_url": f"{base_url.rstrip('/')}/{archive.name}",
                "archive_sha256": digest,
                "archive_size": size,
                "listing": _listing(listings, manifest.plugin_id, out_dir),
            }
        )
    body = {
        "schema": INDEX_SCHEMA,
        "channel": channel,
        # Microseconds, so two publications in one second still order: the app
        # refuses an index signed no later than its cached one unless identical.
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="microseconds"),
        "plugins": rows,
        "blocklist": json.loads(blocklist.read_text(encoding="utf-8")) if blocklist else [],
    }
    payload = (json.dumps(body, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_INDEX_BYTES:  # every client stops reading there and reports too-large
        raise SystemExit(f"the {channel} index is {len(payload)} bytes; clients read at most {MAX_INDEX_BYTES}")
    private = _load_key(key)
    signature = sign(payload, private, key_id)
    parse_index(payload, signature, channel, trusted={key_id: public_key_b64(private)})
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{channel}.json").write_bytes(payload)
    (out_dir / f"{channel}.json.sig").write_bytes(signature)
    print(f"wrote {out_dir / channel}.json with {len(rows)} plugin(s), signed by {key_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plugin_store", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    keygen = sub.add_parser("keygen")
    keygen.add_argument("--key-id", required=True)
    keygen.add_argument("--out", type=Path, required=True)
    hashp = sub.add_parser("hash")
    hashp.add_argument("plugin_dir", type=Path)
    pack = sub.add_parser("pack")
    pack.add_argument("plugin_dir", type=Path)
    pack.add_argument("--out", type=Path, required=True)
    index = sub.add_parser("index")
    index.add_argument("--channel", choices=("stable", "labs"), required=True)
    index.add_argument("--key", type=Path, required=True)
    index.add_argument("--key-id", required=True)
    index.add_argument("--base-url", required=True)
    index.add_argument("--blocklist", type=Path)
    index.add_argument("--listings", type=Path, required=True)
    index.add_argument("--out", type=Path, required=True)
    index.add_argument("archives", type=Path, nargs="*")
    args = parser.parse_args(argv)
    if args.cmd == "keygen":
        return cmd_keygen(args.key_id, args.out)
    if args.cmd == "hash":
        return cmd_hash(args.plugin_dir)
    if args.cmd == "pack":
        return cmd_pack(args.plugin_dir, args.out)
    return cmd_index(
        args.channel, args.archives, args.key, args.key_id, args.base_url, args.out, args.blocklist, args.listings
    )


if __name__ == "__main__":
    raise SystemExit(main())
