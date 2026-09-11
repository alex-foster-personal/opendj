"""PERFMODE + feature-flag gate for on-device stems (issue #1866).

Below the tier floor the control is inert with an explicit tooltip, never a
silent no-op. Agent-native parity: ``python -m apps.stems.local_gate``.
"""

from __future__ import annotations

import json
from typing import Any

from apps.engine_core.perf_tier import local_stems_tier_refusal
from apps.stems.routing import EXECUTOR_LOCAL, resolve_stems_executor

FLAG_ID: str = "local_stems.executor"


def _flag_store() -> Any:
    from apps.feature_flags.store import load_flags
    from apps.shared.platform_paths import DATA_DIR

    return load_flags(DATA_DIR)


def local_stems_gate(*, flag_store: Any | None = None) -> str | None:
    """Why local stems cannot start, or None when the executor may run."""
    if resolve_stems_executor() != EXECUTOR_LOCAL:
        return None
    store = flag_store if flag_store is not None else _flag_store()
    if not store.enabled(FLAG_ID):
        return f"{FLAG_ID} is off for this build (set in feature-flags.json)"
    return local_stems_tier_refusal()


def main(argv: list[str] | None = None) -> int:
    _ = argv
    refusal = local_stems_gate()
    print(json.dumps({"refusal": refusal, "flag_id": FLAG_ID}, indent=2))
    return 0 if refusal is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
