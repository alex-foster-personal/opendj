"""Load the sha256-pinned fleet-af runners inventory v1 snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
V1_PATH = REPO_ROOT / "ci" / "vendor" / "runners.v1.yaml"
V1_SHA256 = "7a4c577252b09cbf8447d6bec4d5985ffb437e720903d3b1aa1932d2426df337"


def raw_bytes() -> bytes:
    data = V1_PATH.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != V1_SHA256:
        raise AssertionError(f"{V1_PATH}: sha256 {digest} != pinned {V1_SHA256}")
    return data


def document() -> dict:
    return yaml.safe_load(raw_bytes())


def pool_labels(name: str) -> list[str]:
    return list(document()["pools"][name])


def pool_label_json(name: str) -> str:
    return json.dumps(pool_labels(name), separators=(",", ":"))


def seats(pool: str) -> list[str]:
    names: list[str] = []
    for host in document()["hosts"]:
        names.extend(
            runner["name"]
            for runner in host["runners"]
            if pool in runner.get("pools", [])
        )
    return names
