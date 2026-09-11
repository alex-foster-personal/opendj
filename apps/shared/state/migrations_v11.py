"""Migration 10 -> 11: per-machine sync credentials (ADR 12 amendment, plan X5).

One table, and deliberately nothing else. No existing table is altered, so
:func:`apps.sync_hub.protocol.sync_digest` produces the same bytes over the
same library before and after this step, exactly as v9 promised for the
ownership tables.

Why a credential at all: after v9 a machine is identified on ``push``,
``pull``, ``status`` and ``digest`` by its ``machine_id`` alone, and the hub
hands every ``machine_id`` it knows to any ``hello`` caller. So a machine id
is not a secret, and an ENFORCE flip that checked ownership by id alone
would be spoofable by anybody who had ever said hello. The credential is the
secret the id is not.

Three readings, stated here so a later reader does not have to diff the spec:

1. **Never synced.** Same reasoning as ``machine_owners``: the hub that
   minted the credential is its only writer, and a credential riding the
   sync set could be pushed by a hostile spoke.
2. **Only the sha256 reaches the database**, like
   ``enrollment_grants.grant_token_sha256`` and
   ``auth_sessions.session_token_sha256``: a stolen hub DB must not hand
   anybody a working bearer. The raw value is returned exactly once, in the
   ``/enroll`` response that minted it.
3. **One live credential per machine** (``machine_id`` is the primary key).
   Re-minting replaces the hash, so the previous bearer stops working the
   moment the new one exists. ``ON DELETE CASCADE`` from ``machines`` means a
   machine row that goes away takes its credential with it.
"""

from __future__ import annotations

_V11: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS machine_credentials (
        machine_id         TEXT PRIMARY KEY
                             REFERENCES machines(machine_id) ON DELETE CASCADE,
        credential_sha256  TEXT NOT NULL UNIQUE,
        minted_at          TEXT NOT NULL
    )
    """,
]

__all__ = ["_V11"]
