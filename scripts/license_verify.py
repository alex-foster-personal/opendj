"""Prove a staged payload carries a real THIRD-PARTY-LICENSES inventory (OSSPUB-05).

Split out of scripts/third_party_licenses.py (600-line ratchet): that module GENERATES the
inventory, this one VERIFIES the staged or mounted result by PARSING it, without regenerating
it. Marker strings and a size floor were not enough (Sol P1, PR #4853 r4171176565): a padded
stub carrying `[python]`, `[javascript]`, `[rust]` and `[bundled]` passed while attributing
nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.license_mirrors import KNOWN_TEXTLESS
from scripts.third_party_licenses import (
    LICENSES_FILE_NAME,
    MIN_LICENSES_FILE_CHARS,
    NOTICE_FILE_NAME,
    ROOT_LICENSE_FILE_NAME,
    LicenseInventoryError,
)

ECOSYSTEMS: tuple[str, ...] = ("python", "javascript", "rust", "bundled")
#: One `render_licenses` component line: "<name> <version> [<ecosystem>] -- <license>".
_RECORD = re.compile(r"^(?P<label>\S.*?) \[(?P<eco>python|javascript|rust|bundled)\] -- \S.*$")
_COUNT = re.compile(r"^Components: (\d+)$", re.MULTILINE)


def component_records(inventory: str) -> list[tuple[str, str]]:
    """(label, ecosystem) per rendered component line; the declared count must match the list."""
    declared = _COUNT.search(inventory)
    section = inventory.partition("\nCOMPONENTS\n")[2].partition("\nLICENSE TEXTS\n")[0]
    records = [(m["label"], m["eco"]) for line in section.splitlines() if (m := _RECORD.match(line))]
    if declared is None or not records or int(declared.group(1)) != len(records):
        raise LicenseInventoryError(
            f"{LICENSES_FILE_NAME} declares {declared.group(1) if declared else 'no'} components "
            f"but lists {len(records)} component records: it was not rendered by the inventory"
        )
    return records


def verify_bundled_licenses(payload_dir: Path) -> None:
    """Prove the PRESENCE of the good thing: parsed component records, each with its license text."""
    for name in (LICENSES_FILE_NAME, NOTICE_FILE_NAME, ROOT_LICENSE_FILE_NAME):
        staged = payload_dir / name
        if not staged.is_file() or staged.stat().st_size == 0:
            raise LicenseInventoryError(f"{staged} is missing or empty: the app would ship without {name}")
    inventory = (payload_dir / LICENSES_FILE_NAME).read_text(encoding="utf-8")
    if len(inventory) < MIN_LICENSES_FILE_CHARS:
        raise LicenseInventoryError(f"{LICENSES_FILE_NAME} is only {len(inventory)} chars: license texts were not rendered")
    records = component_records(inventory)
    for ecosystem in ECOSYSTEMS:
        if not any(eco == ecosystem for _, eco in records):
            raise LicenseInventoryError(f"{LICENSES_FILE_NAME} lists no {ecosystem} component: an inventory failed silently")
    attributed = "\n".join(line for line in inventory.splitlines() if line.startswith("Applies to: "))
    textless_names = {name for _, name in KNOWN_TEXTLESS}
    unattributed = [
        label
        for label, _ in records
        if label.strip() not in attributed and label.rsplit(" ", 1)[0] not in textless_names
    ]
    if unattributed:
        raise LicenseInventoryError(
            f"{len(unattributed)} component(s) carry no rendered license text: {unattributed[:10]}"
        )
