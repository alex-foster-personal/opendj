"""LINK-90 (mock v5 V3-2, M-70): the DJ name this laptop shows on B2B Link.

The first LINK press asks for a name ("A human you link to will see this name");
a signed-in user's account name is saved without asking. The name is what the
other laptop sees on faces, the people list, shout bubbles and the PEER label.

Resolution order (``resolve_label``): the operator's ``MDT_B2B_MACHINE_LABEL``,
else the stored name, else the hostname (which the page never shows: it reads
as "the other laptop" and the panel asks for a name).

Stored in ``<state dir>/b2b-name.json`` next to ``state.db``, written atomically.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

MACHINE_LABEL_ENV = "MDT_B2B_MACHINE_LABEL"
NAME_FILENAME = "b2b-name.json"
NAME_MAX_CHARS = 32
NameSource = Literal["typed", "account"]
LabelSource = Literal["env", "typed", "account", "hostname"]


@dataclass(frozen=True)
class ResolvedLabel:
    name: str
    source: LabelSource
    stored: str | None
    overridden_by_env: bool


def validate_name(raw: str) -> str:
    """The cleaned name, or ValueError saying why it cannot be a DJ name."""
    name = raw.strip()
    if not name:
        raise ValueError("name is empty")
    if len(name) > NAME_MAX_CHARS:
        raise ValueError(f"name is longer than {NAME_MAX_CHARS} characters")
    if "." in name:
        raise ValueError("name contains '.', which reads as a hostname; use a first name or a nickname")
    return name


def read_stored(path: Path) -> tuple[str, NameSource] | None:
    """``(name, source)`` from the store, None when nothing is saved. A corrupt file raises."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    name, source = data.get("name"), data.get("source")
    if not isinstance(name, str) or source not in ("typed", "account"):
        raise ValueError(f"{path} is not a B2B name record: {data!r}")
    return validate_name(name), source


def write_stored(path: Path, name: str, source: NameSource) -> None:
    record = {"name": validate_name(name), "source": source, "saved_at": datetime.now(UTC).isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def clear_stored(path: Path) -> None:
    path.unlink(missing_ok=True)


def resolve_label(path: Path | None) -> ResolvedLabel:
    """Env, else stored, else hostname. ``path`` None means this engine has no state dir."""
    stored = read_stored(path) if path is not None else None
    env = (os.environ.get(MACHINE_LABEL_ENV) or "").strip()
    if env:
        return ResolvedLabel(env, "env", None if stored is None else stored[0], stored is not None)
    if stored is not None:
        return ResolvedLabel(stored[0], stored[1], stored[0], False)
    return ResolvedLabel(socket.gethostname(), "hostname", None, False)
