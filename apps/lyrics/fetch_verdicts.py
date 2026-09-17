"""Persistent line-fetch job outcomes for LRCLIB and ASR (LYRICS-07)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

FETCH_VERDICT_SCHEMA = 1
FETCH_VERDICT_DIRNAME = "lyrics-fetch"

FetchOutcome = Literal["cached", "instrumental", "no_source"]
FetchSource = Literal["lrclib", "asr"]


@dataclass(frozen=True)
class FetchVerdict:
    stable_id: str
    outcome: FetchOutcome
    source: FetchSource | None
    vocals_sha256: str | None
    hub_code: str | None
    hub_message: str | None
    language_iso3: str | None
    recorded_at: str


def verdict_dir(data_dir: Path) -> Path:
    return data_dir / "state" / FETCH_VERDICT_DIRNAME


def verdict_path(data_dir: Path, stable_id: str) -> Path:
    root = verdict_dir(data_dir).resolve()
    path = (root / f"{stable_id}.json").resolve()
    if path.parent != root:
        raise ValueError(f"stable_id escapes lyrics-fetch directory: {stable_id!r}")
    return path


def load_verdict(data_dir: Path, stable_id: str) -> FetchVerdict | None:
    path = verdict_path(data_dir, stable_id)
    if not path.is_file():
        return None
    return _parse(json.loads(path.read_text(encoding="utf-8")), path)


def write_verdict(data_dir: Path, verdict: FetchVerdict) -> None:
    path = verdict_path(data_dir, verdict.stable_id)
    payload = {
        "schema": FETCH_VERDICT_SCHEMA,
        "stable_id": verdict.stable_id,
        "outcome": verdict.outcome,
        "source": verdict.source,
        "vocals_sha256": verdict.vocals_sha256,
        "hub_code": verdict.hub_code,
        "hub_message": verdict.hub_message,
        "language_iso3": verdict.language_iso3,
        "recorded_at": verdict.recorded_at,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, separators=(",", ":"))
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def is_terminal_fresh(verdict: FetchVerdict, current_vocals_sha256: str | None) -> bool:
    """Return whether a stored verdict suppresses another lyrics job attempt."""
    if verdict.outcome in ("cached", "instrumental"):
        return True
    if verdict.outcome == "no_source":
        return verdict.vocals_sha256 == current_vocals_sha256
    return False


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Any, path: Path) -> FetchVerdict:
    if not isinstance(value, dict):
        raise TypeError(f"lyrics-fetch entry {path} must be a JSON object")
    if value.get("schema") != FETCH_VERDICT_SCHEMA:
        raise ValueError(f"lyrics-fetch entry {path} has unsupported schema")
    stable_id = _non_empty_string(value, "stable_id", path)
    outcome = value.get("outcome")
    if outcome not in ("cached", "instrumental", "no_source"):
        raise ValueError(f"lyrics-fetch entry {path} has invalid outcome")
    source = value.get("source")
    if source is not None and source not in ("lrclib", "asr"):
        raise ValueError(f"lyrics-fetch entry {path} has invalid source")
    vocals_sha256 = value.get("vocals_sha256")
    if vocals_sha256 is not None and (
        not isinstance(vocals_sha256, str) or len(vocals_sha256) != 64
    ):
        raise ValueError(f"lyrics-fetch entry {path} has invalid vocals_sha256")
    hub_code = value.get("hub_code")
    if hub_code is not None and not isinstance(hub_code, str):
        raise ValueError(f"lyrics-fetch entry {path} has invalid hub_code")
    hub_message = value.get("hub_message")
    if hub_message is not None and not isinstance(hub_message, str):
        raise ValueError(f"lyrics-fetch entry {path} has invalid hub_message")
    language_iso3 = value.get("language_iso3")
    if language_iso3 is not None and not isinstance(language_iso3, str):
        raise ValueError(f"lyrics-fetch entry {path} has invalid language_iso3")
    recorded_at = _non_empty_string(value, "recorded_at", path)
    return FetchVerdict(
        stable_id=stable_id,
        outcome=outcome,
        source=source,
        vocals_sha256=vocals_sha256,
        hub_code=hub_code,
        hub_message=hub_message,
        language_iso3=language_iso3,
        recorded_at=recorded_at,
    )


def _non_empty_string(entry: dict[str, Any], field: str, path: Path) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"lyrics-fetch entry {path} has invalid {field}")
    return value


__all__ = [
    "FetchOutcome",
    "FetchSource",
    "FetchVerdict",
    "is_terminal_fresh",
    "load_verdict",
    "utc_now_iso",
    "verdict_dir",
    "verdict_path",
    "write_verdict",
]
