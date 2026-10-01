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

from apps.sync_hub import enrollment, service_enroll
from apps.sync_hub.machine_credentials import CredentialVerdict
from apps.sync_hub.machine_wire_limits import (
    MACHINE_ID_MAX_LENGTH,
    MACHINE_NAME_MAX_LENGTH,
)


class MachineModel(BaseModel):
    """One ``machines`` row on the wire."""

    machine_id: str = Field(min_length=1, max_length=MACHINE_ID_MAX_LENGTH)
    name: str = Field(min_length=1, max_length=MACHINE_NAME_MAX_LENGTH)
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
    hash_pending: bool | None = None


class IdentityRejectModel(BaseModel):
    """One identity-collapse rejection: the offered PK lost to a hub survivor."""

    table: str = Field(min_length=1)
    offered_pk: str = Field(min_length=1)
    survivor_pk: str = Field(min_length=1)


#: Carried on every request that can move rows (``hello``, ``push``,
#: ``enroll``) and gated per request, like the capability tokens. ``None`` --
#: the field absent -- is a build from before the wire/schema split, judged by
#: exact schema equality instead (:mod:`apps.sync_hub.wire_version`).
#: Most candidates one ``POST /stale-check`` may carry. The client chunks.
STALE_CHECK_MAX_CANDIDATES: int = 1000

_WIRE_VERSION_FIELD: Any = Field(
    default=None, description="sync wire version; absent on pre-split builds"
)


class SyncErrorBody(BaseModel):
    """The ``detail`` of every sync refusal: a stable code and prose."""

    code: str
    message: str


class SyncErrorResponse(BaseModel):
    """FastAPI wraps an ``HTTPException`` detail under ``detail``."""

    detail: SyncErrorBody


#: What ``hello`` and ``push`` can answer besides 200, for the OpenAPI
#: document and everything generated from it.
SYNC_VERSION_RESPONSES: dict[int | str, dict[str, object]] = {
    409: {
        "model": SyncErrorResponse,
        "description": (
            "The peers must not exchange rows. code: SYNC_WIRE_VERSION (a "
            "different sync wire version), SYNC_SCHEMA_VERSION (a pre-split "
            "peer on a different schema), SYNC_APPLY, SYNC_MACHINE_NAME_TAKEN "
            "or SYNC_UNKNOWN_MACHINE."
        ),
    },
}


class HelloRequest(BaseModel):
    machine: MachineModel
    schema_version: int
    wire_version: int | None = _WIRE_VERSION_FIELD
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
    #: The wire version this hub speaks. The spoke gates on it, not on
    #: ``schema_version``, which only describes the hub's own storage.
    wire_version: int
    seq: int
    #: Every machine this hub knows, peers included. NOT withheld from the
    #: caller, though it hands out other machines' ids (plan X5): a spoke
    #: holds its peers' ``track_locations``, ``sync_policies`` and
    #: ``playlist_pins`` rows, which REFERENCE ``machines``, so a spoke
    #: denied the peer rows would FK-refuse every pulled row that names a
    #: peer (round 2 finding N4). What changed instead is that a machine id
    #: stopped being the credential: under ENFORCE this response only reaches
    #: a caller holding a valid sync credential, and knowing another
    #: machine's id no longer lets anybody act as it.
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
    #: How this hub reads the CALLER's ownership: ``owned``, ``unowned`` or
    #: ``foreign`` (ADR 12). OBSERVE only -- nothing is refused on it. An
    #: unowned machine is told so on every handshake rather than finding out
    #: on the day enforcement is switched on.
    #:
    #: The owner's EMAIL is deliberately NOT here, though the first
    #: implementation pass returned it. ``hello`` is unauthenticated, it
    #: reports on the machine_id in the CALLER's own payload, and the same
    #: response hands back every machine_id this hub knows -- so returning the
    #: email made "which Google account owns machine X" readable by anything
    #: that can open a socket, for every machine in the fleet. That is a NEW
    #: PII disclosure rather than a continuation of the existing tailnet
    #: exposure. ``python -m apps.sync_hub fleet`` answers it where the answer
    #: belongs, behind hub-local access.
    #:
    #: REQUIRED, with no default. Sol review, PR #1648 (P1 BLOCKING): a
    #: default of ``"unowned"`` makes a response this hub failed to compute
    #: indistinguishable from a machine it measured as unowned, at an
    #: identity boundary, and the only construction site (``service.hello``)
    #: passes it explicitly anyway -- so the default could never do anything
    #: except mask a construction bug. Nothing parses this model on the wire
    #: (the spoke reads the raw JSON), so requiring it costs no mixed-version
    #: compatibility.
    ownership: enrollment.OwnershipState
    #: How this hub read the caller's sync credential (plan X5): ``valid``,
    #: ``missing``, ``invalid``, ``revoked`` or ``unowned``. This IS the OBSERVE
    #: report: a machine learns on every handshake that ENFORCE would refuse
    #: it, rather than on the day ENFORCE is switched on. Under ENFORCE only
    #: ``valid`` ever reaches a response; anything else is a 401 first.
    #: Required, no default, for the reason ``ownership`` gives.
    credential: CredentialVerdict
    #: How many live ``tracks`` rows this hub holds, so the caller can tell
    #: SEEDING from MERGING (CLOUDSYNC-07). ``0`` is an affirmative "this
    #: hub holds no library"; ``None`` means a hub too old to answer. It is
    #: REPORTED, not enforced: no spoke refuses a first sync on it since
    #: ADR-0068 (:mod:`apps.sync_hub.capabilities`). Gated by ``library-size/v1`` in
    #: ``capabilities``, which is how a spoke tells the two apart without
    #: guessing from the value.
    #:
    #: A COUNT rather than the set of authoring machines: a migrated library
    #: carries ``origin_device_id IS NULL`` on every row, so attribution
    #: reports nothing and a hub would read its own seeded rows as foreign.
    library_track_count: int | None = None


class EnrollRequest(BaseModel):
    """No owner field, by construction: a request cannot name whose machine
    it is becoming. The owner is read from the credential's own record."""

    machine: MachineModel
    schema_version: int
    wire_version: int | None = _WIRE_VERSION_FIELD
    credential: service_enroll.EnrollCredentialModel


class PushRequest(BaseModel):
    machine_id: str = Field(min_length=1, max_length=MACHINE_ID_MAX_LENGTH)
    schema_version: int
    wire_version: int | None = _WIRE_VERSION_FIELD
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
    #: True when the pusher detected, in this sync, that the hub went
    #: backwards (a new generation token, or a seq below what it pulled) and
    #: is re-offering its library. The stale-copy guard
    #: (:mod:`apps.sync_hub.stale_copy`) stands aside for that push, because
    #: a restored hub has forgotten rows it really held.
    reseed: bool = False


class StaleCandidateModel(BaseModel):
    stable_id: str = Field(min_length=1)
    origin_device_id: str


class StaleCheckRequest(BaseModel):
    """``POST /stale-check``: which of these live tracks did the fleet drop?"""

    machine_id: str = Field(min_length=1, max_length=MACHINE_ID_MAX_LENGTH)
    schema_version: int
    wire_version: int | None = _WIRE_VERSION_FIELD
    candidates: list[StaleCandidateModel] = Field(max_length=STALE_CHECK_MAX_CANDIDATES)
    reseed: bool = False


class StaleCheckResponse(BaseModel):
    #: Candidates another registered machine authored that the hub holds
    #: under no id and no identity remap: the fleet dropped them.
    orphans: list[str]
    #: Foreign candidates whose author is no machine the hub knows. Not
    #: decided either way; the push still carries them.
    unattributable: int = 0


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
    hash_pending: int = 0
    #: Identity-collapse rejections: the offered PK lost to a stored survivor
    #: (issue #3057). Only emitted for ``tracks`` rows where the hub
    #: kept a different PK for the same content identity.
    identity_rejects: list[IdentityRejectModel] = Field(default_factory=list)


class PullResponse(BaseModel):
    rows: list[RowModel]
    seq: int
    #: Every machine this hub knows, for the FK reason ``HelloResponse``
    #: gives; only a credentialed caller reaches it under ENFORCE.
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
    hash_pending: int = 0


class HashPendingResponse(BaseModel):
    stable_ids: list[str]
    total: int
    next_cursor: str | None = None
    schema_version: int
    wire_version: int


class StatusResponse(BaseModel):
    hub_machine_id: str
    schema_version: int
    wire_version: int
    seq: int
    machines: list[MachineModel]
    row_counts: dict[str, int]
    hub_generation: str
    #: True only on a HOSTED hub (``MDT_SYNC_HUB_HOSTED=1``), which checks
    #: each caller's owner against ``entitlement_provider``. A self-hosted
    #: hub reports False and None: it never consults a source.
    hosted: bool
    entitlement_provider: str | None


class SyncRowSampleModel(BaseModel):
    """One sync-eligible row for digest diff (CSSTATUS-09)."""

    pk: list[str] = Field(min_length=1)
    canonical_hex: str = Field(min_length=64, max_length=64)
    updated_at: str | None = None
    origin_device_id: str | None = None
    modified_at: str | None = None


class RowsResponse(BaseModel):
    rows: list[SyncRowSampleModel]
    next_cursor: str | None = None


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
    hash_pending: dict[str, int] = Field(default_factory=dict)


__all__ = [
    "SYNC_VERSION_RESPONSES",
    "DigestResponse",
    "RowsResponse",
    "SyncRowSampleModel",
    "EnrollRequest",
    "HashPendingResponse",
    "HelloRequest",
    "HelloResponse",
    "IdentityRejectModel",
    "MachineModel",
    "PullResponse",
    "PushRequest",
    "PushResponse",
    "RowModel",
    "STALE_CHECK_MAX_CANDIDATES",
    "StaleCandidateModel",
    "StaleCheckRequest",
    "StaleCheckResponse",
    "StatusResponse",
    "SyncErrorBody",
    "SyncErrorResponse",
]
