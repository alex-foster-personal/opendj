"""Migration 8 -> 9: machine enrollment (``specs/design_decision_12.md``).

Two tables, and deliberately nothing else. No existing table is altered, so
:func:`apps.sync_hub.protocol.sync_digest` produces the same bytes over the
same library before and after this step, and every sync payload keeps its
current shape. That property is the reason ownership lives in a link table
rather than as a column on ``machines``, and
``tests/cloudsync/test_enrollment_schema_v9.py`` asserts it directly.

Its own module rather than more lines in :mod:`apps.shared.state.schema`,
which is already at 626 lines against the 600-line review gate in
``scripts/quality_gate.py``. Same split, same reason, as
``apps.database.table_docs``.

Three readings worth stating, because a later reader will otherwise have to
diff the ADR:

1. **Neither table is synced.** Ownership is asserted only by a call that
   carried a live credential to the hub that holds the row. If it rode the
   sync set, a restored or hostile spoke could push ownership rows and adopt
   machines under LWW like any other row. The hub that performed the
   enrollment is the only writer, and ``apps.sync_hub.enrollment`` is the
   only module that writes it.
2. **``hub_machine_id`` is on every owner row on purpose.** A hub restored
   from another machine's backup inherits this whole table, but NOT its
   ``machine-id`` file, which lives outside the DB (ADR 05 section 1). A row
   whose ``hub_machine_id`` is not this hub's live id is reported as FOREIGN
   and the machine reads as unowned. That is an invariant re-derived on every
   read, not a recorded value that can go stale.
3. **Only the sha256 of a grant reaches the database.** Same shape and same
   reasoning as ``auth_sessions.session_token_sha256``: a stolen DB must not
   hand anybody a redeemable credential. The raw token is returned exactly
   once, to the operator who minted it.
"""
from __future__ import annotations

#: The ``machine_owners.enrolled_via`` vocabulary, as the CHECK enforces it.
#: Imported by :mod:`apps.sync_hub.enrollment` so the provenance list has one
#: home rather than a copy that can drift from the constraint.
ENROLLED_VIA_VALUES: tuple[str, ...] = ("grant", "google_id_token", "adopt")

_ENROLLED_VIA_SQL: str = ",".join(f"'{value}'" for value in ENROLLED_VIA_VALUES)

_V9: list[str] = [
    # One live row per machine names its owner. PK is machine_id, so a shared
    # or household machine needs a further migration (ADR 12 consequence 4).
    f"""
    CREATE TABLE IF NOT EXISTS machine_owners (
        machine_id      TEXT PRIMARY KEY
                          REFERENCES machines(machine_id) ON DELETE CASCADE,
        google_sub      TEXT NOT NULL
                          REFERENCES users(google_sub) ON DELETE CASCADE,
        hub_machine_id  TEXT NOT NULL,
        enrolled_at     TEXT NOT NULL,
        enrolled_via    TEXT NOT NULL CHECK
                          (enrolled_via IN ({_ENROLLED_VIA_SQL})),
        revoked_at      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_machine_owners_sub "
    "ON machine_owners(google_sub)",
    # A short-lived single-use credential minted by an authenticated operator
    # on the hub and carried, once, to the machine that is joining.
    """
    CREATE TABLE IF NOT EXISTS enrollment_grants (
        grant_token_sha256  TEXT PRIMARY KEY,
        google_sub          TEXT NOT NULL
                              REFERENCES users(google_sub) ON DELETE CASCADE,
        created_at          TEXT NOT NULL,
        expires_at          TEXT NOT NULL,
        redeemed_at         TEXT,
        redeemed_machine_id TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_enrollment_grants_expires "
    "ON enrollment_grants(expires_at)",
]

__all__ = ["ENROLLED_VIA_VALUES", "_V9"]
