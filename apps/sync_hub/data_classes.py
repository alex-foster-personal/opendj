"""CloudSync data-class registry: everything that syncs, and everything that does not.

One :class:`DataClass` per class of data: every table on both schema ladders,
every ``sync_policies.asset_kind``, the caches, files, secrets, logs and the
git repo itself, each with how it moves between machines and why.

    uv run python -m apps.sync_hub.data_classes --markdown   # writes the spec doc
    uv run python -m apps.sync_hub.data_classes --json       # agent-readable dump

The markdown lands at ``specs/cloudsync-data-classes.md``; a test pins the
committed copy equal to :func:`render_markdown`, so it cannot go stale.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from apps.shared.platform_paths import PROJECT_ROOT
from apps.sync_hub.data_classes_files import IGNORED_PATHS, NON_TABLE_CLASSES
from apps.sync_hub.data_classes_tables import TABLE_CLASSES
from apps.sync_hub.data_classes_types import MECHANISMS, DataClass, IgnoredPath

DATA_CLASSES: tuple[DataClass, ...] = (*TABLE_CLASSES, *NON_TABLE_CLASSES)

DOC_PATH: Path = PROJECT_ROOT / "specs" / "cloudsync-data-classes.md"


class DataClassLookupError(KeyError):
    """A table or asset kind maps to zero or several classes. Never swallowed."""


# ----- lookups ---------------------------------------------------------------


def classes_by_id(classes: Iterable[DataClass] = DATA_CLASSES) -> dict[str, DataClass]:
    """Index ``classes`` by id; a duplicate id raises rather than shadowing."""
    index: dict[str, DataClass] = {}
    for data_class in classes:
        if data_class.id in index:
            raise ValueError(f"duplicate data class id {data_class.id!r}")
        index[data_class.id] = data_class
    return index


def _owners(key: str, attribute: str, classes: Iterable[DataClass]) -> list[str]:
    """Ids of the classes whose ``attribute`` covers ``key``."""
    if attribute == "table":
        return [c.id for c in classes if key in c.tables()]
    return [c.id for c in classes if c.asset_kind == key]


def coverage_problems(
    keys: Iterable[str], attribute: str, classes: Iterable[DataClass] = DATA_CLASSES
) -> list[str]:
    """Every ``key`` (a table or asset kind) not owned by exactly one class.

    ``attribute`` is ``"table"`` or ``"asset_kind"``. Empty means every key
    maps to exactly one class.
    """
    if attribute not in ("table", "asset_kind"):
        raise ValueError(f"attribute must be 'table' or 'asset_kind', got {attribute!r}")
    pool = tuple(classes)
    problems: list[str] = []
    for key in sorted(set(keys)):
        owners = _owners(key, attribute, pool)
        if len(owners) != 1:
            problems.append(f"{attribute} {key!r} maps to {len(owners)} classes: {owners}")
    return problems


def class_for_table(table: str, classes: Iterable[DataClass] = DATA_CLASSES) -> DataClass:
    """The one class owning ``table``; raises when zero or several do."""
    pool = tuple(classes)
    owners = _owners(table, "table", pool)
    if len(owners) != 1:
        raise DataClassLookupError(f"table {table!r} maps to {len(owners)} classes: {owners}")
    return classes_by_id(pool)[owners[0]]


def class_for_asset_kind(kind: str, classes: Iterable[DataClass] = DATA_CLASSES) -> DataClass:
    """The one class keyed by asset kind ``kind``; raises when zero or several are."""
    pool = tuple(classes)
    owners = _owners(kind, "asset_kind", pool)
    if len(owners) != 1:
        raise DataClassLookupError(f"asset kind {kind!r} maps to {len(owners)} classes: {owners}")
    return classes_by_id(pool)[owners[0]]


# ----- markdown ----------------------------------------------------------------

_MECHANISM_TITLES: dict[str, str] = {
    "sync_hub_changelog": "Syncs row by row through the hub changelog",
    "sync_hub_registry": "Syncs through the machines registry snapshot",
    "r2_asset_tier": "Syncs as R2 assets, per-machine configurable",
    "git": "Moves with the git repo",
    "machine_local": "Never leaves the machine (by design)",
    "not_yet_built": "Should sync, nothing moves it yet",
}

_PENDING_DECISIONS = """## Pending decisions

- **Policy authority (D4b, pending the maintainer).** Two authorities exist today:
  the synced `sync_policies` / `playlist_pins` tables, and the file-based
  `apps/cloud/policy.py` (`cloudsync-policy.json`, cloud|local mode plus
  machine-class defaults that never reach `sync_policies`). This registry and
  `apps/sync_hub/policy_rules.py` implement the recommendation: the synced
  `sync_policies` table is the SINGLE authority, and the `policy.py`
  machine-class defaults become seed input only (an explicit seed verb
  writes them as ordinary rows; nothing reads them at runtime). `policy.py`
  keeps only the entitlement boundary (cloud|local).
- **Library tables stay non-configurable (D4e).** Implemented as a
  validator error: a sync-set table cannot be excluded by any machine.
- **Unbuilt classes (D2).** Every row under "Should sync, nothing moves it
  yet" needs an owner call before it joins `SYNC_TABLES`.
"""


def _cell(text: str) -> str:
    """Escape a value for one markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _row(data_class: DataClass) -> str:
    where = "<br>".join(f"{loc.kind}: `{loc.where}`" for loc in data_class.storage)
    modes = ", ".join(data_class.allowed_modes) if data_class.allowed_modes else "fixed"
    configurable = "yes" if data_class.configurable_per_machine else "no"
    how: str = data_class.mechanism
    if data_class.asset_kind is not None:
        how = f"{how} (asset_kind `{data_class.asset_kind}`)"
    title = f"**{data_class.title}** (`{data_class.id}`)"
    cells = (title, where, how, configurable, modes, data_class.reason)
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _ignored_row(ignored: IgnoredPath) -> str:
    covered_by = f"`{ignored.data_class}`" if ignored.data_class else "-"
    return f"| `{_cell(ignored.pattern)}` | {ignored.kind} | {covered_by} |"


def render_markdown(
    classes: Iterable[DataClass] = DATA_CLASSES,
    ignored: Iterable[IgnoredPath] = IGNORED_PATHS,
) -> str:
    """The committed spec doc, deterministically, from the registry alone."""
    pool = tuple(classes)
    lines = [
        "# CloudSync data classes: what syncs and what stays offline",
        "",
        "GENERATED by `uv run python -m apps.sync_hub.data_classes --markdown` from",
        "`apps/sync_hub/data_classes*.py`. Do not edit by hand:",
        "`tests/cloudsync/test_data_classes.py` fails when this file differs from",
        "the generator. `<data>` is the data dir (MDT_DATA_DIR), `<repo>` the checkout.",
        "",
        f"{len(pool)} classes. Modes apply only where per-machine configurable:",
        "pinned (always local), cached (local up to cache_budget_mb, evictable),",
        "stream (fetched from R2 on demand), excluded (never on this machine).",
        "",
    ]
    for mechanism in MECHANISMS:
        members = [c for c in pool if c.mechanism == mechanism]
        if not members:
            continue
        lines += [
            f"## {_MECHANISM_TITLES[mechanism]} (`{mechanism}`, {len(members)})",
            "",
            "| Data | Where it lives | Syncs how | Per-machine configurable | Modes | Why |",
            "|---|---|---|---|---|---|",
            *(_row(c) for c in members),
            "",
        ]
    lines += [
        _PENDING_DECISIONS,
        "## Gitignored paths",
        "",
        "Every non-negated `.gitignore` pattern. `user_data` rows name the class that",
        "covers them; `runtime_state` is locks, logs and agent bookkeeping.",
        "",
        "| Pattern | Kind | Covered by class |",
        "|---|---|---|",
        *(_ignored_row(i) for i in ignored),
        "",
    ]
    return "\n".join(lines)


# ----- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``--markdown`` writes the spec doc; ``--json`` prints the registry."""
    parser = argparse.ArgumentParser(prog="python -m apps.sync_hub.data_classes")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--markdown", action="store_true", help=f"write {DOC_PATH}")
    group.add_argument("--json", action="store_true", help="print the registry as JSON")
    parser.add_argument("--out", type=Path, default=DOC_PATH, help="markdown destination")
    args = parser.parse_args(argv)
    if args.markdown:
        args.out.write_text(render_markdown(), encoding="utf-8")
        print(f"[OK] wrote {len(DATA_CLASSES)} classes to {args.out}")
        return 0
    print(json.dumps(registry_payload(), indent=2, sort_keys=True))
    return 0


def registry_payload() -> dict[str, list[dict[str, object]]]:
    """The registry as JSON-safe data: ``--json``, ``policy classes`` and
    ``GET /api/v1/cloudsync/data-classes`` all emit exactly this."""
    return {
        "classes": [asdict(c) for c in DATA_CLASSES],
        "ignored_paths": [asdict(i) for i in IGNORED_PATHS],
    }


if __name__ == "__main__":
    raise SystemExit(main())
