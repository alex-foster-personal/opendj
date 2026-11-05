"""Import contract for play-order event helpers without Phase 5 state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EVENT_HELPER_MODULE_FILES = (
    "apps/__init__.py",
    "apps/shared/lazy_event_bus.py",
    "apps/shared/play_orders/_time.py",
    "apps/shared/play_orders/events.py",
    "apps/shared/playlist_sets/events.py",
)


def _write_isolated_event_helper_package(root: Path) -> None:
    """Copy only the event-helper package chain into ``root`` (no ``apps.shared.state``)."""
    for rel in _EVENT_HELPER_MODULE_FILES:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((_REPO_ROOT / rel).read_bytes())
    # Minimal package inits: the real ones re-export store/schema and would
    # drag in modules this layout deliberately omits.
    (root / "apps/shared/play_orders/__init__.py").write_text(
        '"""Isolated play_orders package stub for import-contract tests."""\n',
        encoding="utf-8",
    )
    (root / "apps/shared/playlist_sets/__init__.py").write_text(
        '"""Isolated playlist_sets package stub for import-contract tests."""\n',
        encoding="utf-8",
    )


def _import_event_modules_in_subprocess(isolated_root: Path) -> dict[str, object]:
    """Import event helpers in a fresh interpreter with no Phase 5 state package on disk."""
    code = (
        "import importlib\n"
        "import json\n"
        "import sys\n"
        "mods = []\n"
        "for name in ('apps.shared.play_orders.events', 'apps.shared.playlist_sets.events'):\n"
        "    mods.append(importlib.import_module(name).__name__)\n"
        "print(json.dumps({\n"
        "    'modules': mods,\n"
        "    'state_imported': 'apps.shared.state' in sys.modules,\n"
        "}))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(isolated_root)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(isolated_root),
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


def test_event_modules_import_without_apps_shared_state(tmp_path: Path) -> None:
    """[if] Phase 5 state is absent [then] event helpers import, [else stop]."""
    isolated_root = tmp_path / "isolated-apps"
    _write_isolated_event_helper_package(isolated_root)
    assert not (isolated_root / "apps" / "shared" / "state").exists()

    result = _import_event_modules_in_subprocess(isolated_root)
    assert result["modules"] == [
        "apps.shared.play_orders.events",
        "apps.shared.playlist_sets.events",
    ]
    assert result["state_imported"] is False
