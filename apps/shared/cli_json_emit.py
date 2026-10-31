"""Small CLI helpers shared by backup-style entrypoints."""
from __future__ import annotations

import json


def emit_json_payload(payload: dict[str, object]) -> None:
    print(json.dumps(payload, default=str, sort_keys=True))
