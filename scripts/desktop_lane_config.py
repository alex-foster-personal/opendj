"""Derive the per-lane Tauri bundle overlay for the Open DJ desktop shell.

The bake-off is over (agentB lane landed Sat 29 Aug 2026; suffix dropped per
OPS-08), so the DEFAULT build is the plain product: "Open DJ",
com.opendj.desktop, and an Application Support dir of the same name. The
lane machinery stays for future bake-offs: when two lanes install on the
SAME Mac every macOS-visible name has to differ, and the bundle identifier
is the clash key (macOS derives the per-app Application Support, Caches and
WebKit storage paths from it). productName is the second key, because it
names the .app on disk, the window, and the dmg volume.

The label is NOT hardcoded anywhere in the tree. It comes from
``MDT_LANE_LABEL`` in the worktree's .env, the same pattern as
``MUSIC_DJ_BACKEND_PORT`` and ``MDT_DATA_DIR``. Unset means "Open DJ",
which is the product's real name rather than a hidden default.

Requirements:

- ✔︎ ✅ Unset or blank label yields the unmodified product name and
  identifier. -> :func:`overlay`
- ✔︎ ✅ A label yields "Open DJ (B)", "com.opendj.desktop.lane-b" and a
  shell-safe dmg filename. -> :func:`overlay`, :func:`dmg_filename`
- ✔︎ ✅ A label that would produce an invalid identifier segment or an
  unsafe filename is refused, never sanitised. -> :func:`validate_label`

Acceptance tests:

- [if] the label is unset [then] the identifier stays ``com.opendj.desktop``
  and the overlay is empty, [else ⛔️].
- [if] the label is ``B`` [then] the identifier ends ``.lane-b`` and the
  product name ends ``(B)``, [else ⛔️].
- [if] the label is ``b b``, ``../x`` or ``-b`` [then] the call raises
  :class:`LaneLabelError`, [else ⛔️].
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# A CFBundleIdentifier segment is alphanumeric plus hyphen, and Apple
# treats it case-insensitively. Anything else is refused rather than
# rewritten, so a typo in .env cannot silently ship under a name nobody
# expected.
LABEL_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9]+$")

LANE_SEGMENT_PREFIX: str = "lane-"


class LaneLabelError(ValueError):
    """The lane label cannot be turned into a safe bundle name."""


def validate_label(raw: str | None) -> str | None:
    """Return a normalised label, or ``None`` when no label is set."""
    if raw is None:
        return None
    stripped = raw.strip()
    if stripped == "":
        return None
    if not LABEL_PATTERN.match(stripped):
        raise LaneLabelError(
            f"MDT_LANE_LABEL={raw!r} is not usable: it must be letters and "
            "digits only (it becomes a bundle identifier segment, a window "
            "title and a filename)"
        )
    return stripped


def lane_identifier(base_identifier: str, label: str | None) -> str:
    """Suffix the bundle identifier, which is the macOS clash key."""
    if label is None:
        return base_identifier
    return f"{base_identifier}.{LANE_SEGMENT_PREFIX}{label.lower()}"


def lane_product_name(base_product_name: str, label: str | None) -> str:
    """Label the on-disk .app name, the window title and the dmg volume."""
    if label is None:
        return base_product_name
    return f"{base_product_name} ({label})"


def product_slug(base_product_name: str) -> str:
    """A shell-safe stem for the artifact, so testers never quote a path."""
    slug = re.sub(r"[^A-Za-z0-9]", "", base_product_name)
    if slug == "":
        raise LaneLabelError(
            f"productName {base_product_name!r} has no alphanumeric characters"
        )
    return slug


def overlay(
    base_product_name: str, base_identifier: str, label: str | None
) -> dict[str, str]:
    """The ``tauri build --config`` overlay for this lane.

    Empty when there is no label: an empty overlay is a no-op merge, so the
    unlabelled build is byte-for-byte the same command as before.
    """
    if label is None:
        return {}
    return {
        "productName": lane_product_name(base_product_name, label),
        "identifier": lane_identifier(base_identifier, label),
    }


def dmg_filename(
    base_product_name: str, label: str | None, version: str, arch: str
) -> str:
    """Name the artifact ``OpenDJ-B-0.1.0-aarch64.dmg``.

    Tauri names its own output from productName, which yields spaces and
    parentheses once a label is applied. This form keeps every downstream
    command (scp, curl, shell loops) free of quoting traps.

    ``arch`` is passed in by the caller. It used to be parsed out of the
    filename of the dmg Tauri bundled, but that bundle target is gone
    (#1711), so an empty value is refused rather than joined into a name
    like ``OpenDJ-B-0.1.0-.dmg`` that no machine can be told from.
    """
    if not arch:
        raise LaneLabelError(
            "an artifact name needs an architecture; the recipe declares "
            "arm64 only (justfile, ARM64 ONLY v1 decision)"
        )
    stem = product_slug(base_product_name)
    parts = [stem] if label is None else [stem, label]
    parts.extend([version, arch])
    return f"{'-'.join(parts)}.dmg"


# ----- CLI ---------------------------------------------------------------
def _load_conf(config_path: Path) -> tuple[str, str, str]:
    conf = json.loads(config_path.read_text(encoding="utf-8"))
    missing = [key for key in ("productName", "identifier", "version") if key not in conf]
    if missing:
        raise LaneLabelError(f"{config_path} is missing {', '.join(missing)}")
    return conf["productName"], conf["identifier"], conf["version"]


def manifest_stamp(manifest_path: Path, field: str) -> str:
    """Read ONE identity field out of a built payload's manifest.

    The dmg recipe stamps the Tauri binary from the manifest the payload
    builder just wrote, rather than running its own ``git`` call. That is the
    whole point: two independent reads can disagree, and a shell that claims a
    different commit from the engine it ships with is the confusion this
    train exists to remove. Booleans are emitted as 1/0 because the value is
    read back by ``option_env!`` in Rust, where "False" is a true-ish string.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        raise LaneLabelError(f"{manifest_path} has no identity block")
    if field not in identity:
        raise LaneLabelError(
            f"{manifest_path} identity has no {field!r}; it has "
            f"{', '.join(sorted(identity))}"
        )
    value = identity[field]
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        raise LaneLabelError(
            f"{manifest_path} identity.{field} is null; the payload builder "
            "must fail rather than stamp an unknown value"
        )
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("emit", choices=["overlay", "dmg-name", "stamp"])
    parser.add_argument("--config", default=None, type=Path)
    parser.add_argument("--label", default=None)
    parser.add_argument(
        "--arch",
        default=None,
        help="artifact architecture, e.g. aarch64; required for dmg-name",
    )
    parser.add_argument(
        "--manifest", default=None, type=Path, help="payload manifest; required for stamp"
    )
    parser.add_argument("--field", default=None, help="identity field; required for stamp")
    args = parser.parse_args(argv)

    if args.emit == "stamp":
        if args.manifest is None or args.field is None:
            raise LaneLabelError("stamp needs --manifest and --field")
        print(manifest_stamp(args.manifest, args.field))
        return 0

    if args.config is None:
        raise LaneLabelError(f"{args.emit} needs --config")
    product_name, identifier, version = _load_conf(args.config)
    label = validate_label(args.label)

    if args.emit == "overlay":
        print(json.dumps(overlay(product_name, identifier, label), sort_keys=True))
    elif args.emit == "dmg-name":
        if args.arch is None:
            raise LaneLabelError("dmg-name needs --arch to name the artifact")
        print(dmg_filename(product_name, label, version, args.arch))
    return 0


if __name__ == "__main__":
    sys.exit(main())
