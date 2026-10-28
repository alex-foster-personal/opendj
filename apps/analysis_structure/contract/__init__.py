"""Public contract of ``apps.analysis_structure`` (ADR-NEW-module-contracts).

Code outside the package reaches song structure only through this module (and,
for delivery layers, ``apps.analysis_structure.http``). It imports only stdlib,
pydantic and typing so importing it never pulls in the pipeline.

Today the one outside operation is the serve-time merge of own sections into an
``/anlz`` payload's ``phrases`` field (STRUCT-02, STRUCT-04). The payload stays a
plain dict on the wire; ``OwnPhrasesStatus`` documents the ``own_phrases`` block
that merge adds, without changing what is served.

-Claude
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, JsonValue

CONTRACT_VERSION = "analysis_structure/3"  # /3: the payload is typed JSON, not Any; /2: "own-fast" (STRUCT-06)

PhraseSourceLane = Literal["rbx", "own"]
"""The PARITY-02 phrase selection lane a request asks for."""

PHRASE_SOURCE_LANES: tuple[str, ...] = ("rbx", "own")

PhrasesSourceName = Literal["rekordbox", "own", "own-fast", "none"]
"""The ``phrases_source`` value a merged payload names."""


class OwnPhrasesStatus(BaseModel):
    """The ``own_phrases`` block: the sidecar's status, reported, never hidden."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    reason: str | None = None
    producer: str | None = None
    producer_version: str | None = None


@runtime_checkable
class StructureService(Protocol):
    """The operations outside code uses on song structure today."""

    def merge_own_phrases(
        self, payload: dict[str, JsonValue], stable_id: str, data_dir: Path, *, source: str, share: bool = False
    ) -> dict[str, JsonValue]:
        """Return ``payload`` with ``phrases``/``phrases_source`` (and ``own_phrases``) merged.

        ``share`` is the request's audio audience; a share request never starts
        own-fast work, which reads only the local copy.

        Raises ``ValueError`` for a ``source`` outside ``PHRASE_SOURCE_LANES``.
        """
        ...


__all__ = [
    "CONTRACT_VERSION",
    "PHRASE_SOURCE_LANES",
    "OwnPhrasesStatus",
    "PhraseSourceLane",
    "PhrasesSourceName",
    "StructureService",
]
