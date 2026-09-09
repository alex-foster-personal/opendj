"""The request and response bodies of :mod:`apps.sync_hub.service`.

Split out of that module (quality-gate file_size ratchet, round 5 gate B-1,
which added a capability field to three of them and a gate to the handlers
that read it). The standard FastAPI shape: this module is the CONTRACT --
what a peer may send and what it gets back, with no behavior attached -- and
``service`` is the handlers. Every name here is re-exported from ``service``,
so an existing ``service.PushRequest`` reference keeps working.

The three ``capabilities`` fields are the round 5 gate B-1 negotiation; see
:mod:`apps.sync_hub.capabilities` for why the advertisement rides every
request rather than being remembered from ``hello``.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MachineModel(BaseModel):
    """One ``machines`` row on the wire."""

    machine_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    is_hub: bool = False
    data_root: str | None = None
    first_seen: str = Field(min_length=1)
    last_seen: str = Field(min_length=1)


class RowModel(BaseModel):
    """One offered row. ``members`` is set only on a ``playlists`` row."""

    table: str = Field(min_length=1)
    pk: list[str] = Field(min_length=1)
    values: dict[str, Any]
    members: list[dict[str, Any]] | None = None


class HelloRequest(BaseModel):
    machine: MachineModel
    schema_version: int
    #: Protocol features the CALLER understands (round 5 gate B-1). Absent on
    #: any build before this one. Discovery only -- ``hello`` never answers
    #: partially, so nothing here is gated on it; the endpoints that CAN
    #: answer partially read the advertisement off their own request.
    capabilities: list[str] = Field(default_factory=list)
    #: The fleet as the caller knows it (round 2 finding N4). Merged after
    #: ``machine`` so a hub restored to a point before some peer first said
    #: hello learns that peer from whoever still remembers it, instead of
    #: 409ing every row that references it.
    machines: list[MachineModel] = Field(default_factory=list)


class HelloResponse(BaseModel):
    hub_machine_id: str
    schema_version: int
    seq: int
    machines: list[MachineModel]
    #: This hub's generation token (round 2 finding N6). It changes when the
    #: hub's DB moves backwards under a data dir that did not -- a restore --
    #: and NOT when its changelog is pruned. A spoke that sees a different
    #: token than the one it stored resets both sync floors.
    hub_generation: str
    #: Protocol features THIS HUB understands. A spoke reads it to learn
    #: whether its hub is upgraded; absent means a hub on ``origin/main`` or
    #: earlier, which is not the same as an upgraded hub advertising nothing.
    capabilities: list[str] = Field(default_factory=list)


class PushRequest(BaseModel):
    machine_id: str = Field(min_length=1)
    schema_version: int
    rows: list[RowModel]
    #: The pusher's ``machines`` snapshot, merged before the rows are applied
    #: (round 2 finding N4, round 1 A4). ``sync_policies``, ``playlist_pins``
    #: and ``track_locations`` all carry a ``machine_id`` REFERENCES
    #: ``machines``, and every spoke holds rows belonging to its peers, so a
    #: hub that has not met one of them refused the whole push with a
    #: FOREIGN KEY 409 that re-fired on every retry. Empty means the caller
    #: offered no snapshot, which is only safe when its rows name machines
    #: this hub already knows.
    machines: list[MachineModel] = Field(default_factory=list)
    #: Protocol features the pusher understands (round 5 gate B-1). Empty or
    #: absent means this hub must refuse anything it cannot fully decide,
    #: rather than report a shortfall the caller has no field to read.
    capabilities: list[str] = Field(default_factory=list)


class PushResponse(BaseModel):
    accepted: int
    rejected: int
    seq: int
    #: Offered rows the hub REFUSED because its own local copy carries a
    #: stamp it cannot order (round 5). Distinct from ``rejected``, which
    #: means the row lost a comparison that actually happened: a quarantined
    #: row was never compared and the hub's copy is untouched.
    #: ``accepted + rejected + quarantined`` equals the rows offered.
    quarantined: int = 0


class PullResponse(BaseModel):
    rows: list[RowModel]
    seq: int
    machines: list[MachineModel]
    #: True when the hub still holds changelog entries above ``seq``. The
    #: client loops on it rather than inferring "done" from an empty page:
    #: dedup means a chunk can legitimately return fewer rows than entries.
    has_more: bool = False
    #: Changelog entries in this chunk whose row is gone from the hub
    #: (round 2 finding 4a). Non-zero means something hard-deleted a synced
    #: row on the hub; the pull still serves everything else.
    skipped: int = 0
    #: Rows in this chunk the hub HOLDS but could not offer, because a stored
    #: stamp on them cannot be ordered (round 5). Non-zero means the hub
    #: needs ``python -m apps.shared.state.normalize_stamps --live``; the
    #: pull still serves everything else.
    quarantined: int = 0


class StatusResponse(BaseModel):
    hub_machine_id: str
    schema_version: int
    seq: int
    machines: list[MachineModel]
    row_counts: dict[str, int]
    hub_generation: str


class DigestResponse(BaseModel):
    tables: dict[str, str]
    overall: str
    #: The ``hub_changelog`` position this digest describes, read in the same
    #: transaction as the hashes (round 2 finding 6b). A spoke whose pull
    #: stopped below this seq knows a third machine pushed in the gap, and
    #: that a difference here is not divergence.
    seq: int
    #: Rows per table the hub EXCLUDED from the hash because a stored stamp
    #: cannot be ordered (round 5). Absent tables quarantined nothing. Not
    #: folded into ``overall``: two peers with identical eligible content
    #: have converged, and folding it in would fire the ADR 04 c6 corruption
    #: alarm on ordinary legacy data.
    quarantined: dict[str, int] = Field(default_factory=dict)


__all__ = [
    "DigestResponse",
    "HelloRequest",
    "HelloResponse",
    "MachineModel",
    "PullResponse",
    "PushRequest",
    "PushResponse",
    "RowModel",
    "StatusResponse",
]
