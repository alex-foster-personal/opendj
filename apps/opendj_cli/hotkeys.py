"""``opendj hotkeys [--platform mac|other] [--json]``: the hotkeys overlay's rows (#12287).

Rows come from ``hotkeys_contract.json``, which the frontend's
``scripts/export-hotkeys-contract.mjs`` writes from the overlay registry, already
rendered per platform by the overlay's formatter; a frontend test fails when it
drifts. A row gated by a build flag is listed but marked unavailable while the
flag is off. Flag state comes from this machine's flag file (``load_flags``, as
the daemon reads it at startup), so no engine is needed; an undeclared flag raises.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from apps.feature_flags import FlagStore, load_flags
from apps.opendj_cli import EXIT_CONFIRMED
from apps.shared.platform_paths import DATA_DIR, PROJECT_ROOT

CONTRACT_PATH = PROJECT_ROOT / "apps" / "opendj_cli" / "hotkeys_contract.json"
PLATFORMS = ("mac", "other")
HOTKEYS_USAGE = "opendj hotkeys [--platform mac|other] [--json]"


@dataclass(frozen=True)
class HotkeyRow:
    """One overlay row as one platform shows it, with whether it works right now."""

    id: str
    chord: str
    description: str
    group: str
    feature_flag: str | None
    available: bool


# ----- contract ---------------------------------------------------------------


def host_platform() -> str:
    """The contract platform for this interpreter: ``mac`` on macOS, ``other`` everywhere else."""
    return "mac" if sys.platform == "darwin" else "other"


def load_contract(path: Path = CONTRACT_PATH) -> list[dict[str, Any]]:
    """The exported overlay rows; an empty or malformed export raises."""
    rows = json.loads(path.read_text(encoding="utf-8")).get("rows")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{path} has no rows[]; rerun scripts/export-hotkeys-contract.mjs")
    return rows


def hotkey_rows(contract: Sequence[dict[str, Any]], platform: str, flags: FlagStore) -> list[HotkeyRow]:
    """Contract rows rendered for ``platform``, each marked available unless its flag is off."""
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {PLATFORMS}, got {platform!r}")
    rows: list[HotkeyRow] = []
    for row in contract:
        flag_id = row["feature_flag"]
        rendered = row[platform]
        rows.append(
            HotkeyRow(
                id=row["id"],
                chord=rendered["chord"],
                description=rendered["description"],
                group=row["group"],
                feature_flag=flag_id,
                available=flag_id is None or flags.enabled(flag_id),
            )
        )
    return rows


# ----- output -----------------------------------------------------------------


def _text(rows: Sequence[HotkeyRow]) -> str:
    """One aligned ``id  chord  description`` line per row; an unavailable row says why."""
    id_width = max(len(row.id) for row in rows)
    chord_width = max(len(row.chord) for row in rows)
    lines = []
    for row in rows:
        line = f"{row.id:<{id_width}}  {row.chord:<{chord_width}}  {row.description}"
        if not row.available:
            line += f"  [unavailable: feature flag {row.feature_flag} is off]"
        lines.append(line)
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opendj hotkeys",
        usage=HOTKEYS_USAGE,
        description=(
            "List the hotkeys overlay's shortcut rows: id, chord and description. "
            "Rows behind a build flag that is off are marked unavailable."
        ),
    )
    parser.add_argument(
        "--platform",
        choices=PLATFORMS,
        default=host_platform(),
        help="modifier labels to show: mac (Cmd) or other (Ctrl); default is this machine's platform",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def run(argv: Sequence[str], *, as_json: bool = False) -> int:
    """Entry point from ``opendj hotkeys ...``: print every overlay row for one platform."""
    args = _parser().parse_args(list(argv))
    rows = hotkey_rows(load_contract(), args.platform, load_flags(DATA_DIR))
    if as_json or args.json:
        document = {"platform": args.platform, "rows": [asdict(row) for row in rows]}
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        print(_text(rows))
    return EXIT_CONFIRMED
