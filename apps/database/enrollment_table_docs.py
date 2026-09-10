"""Curated prose for the migration v9 enrollment tables (ADR 12).

Its own module, in the pattern :mod:`apps.database.table_docs` established:
``column_docs.py`` sits at 576 lines against the 600-line review gate in
``scripts/quality_gate.py``, so a new table's entries go beside it rather
than inside it.

Not optional. :func:`apps.shared.state.db.open_rw` regenerates the state
DB's travelling AGENTS.md whenever migrations advance the schema, and
:class:`apps.database.generate_agents_md.MissingColumnDocsError` makes an
undocumented live column fail that regeneration -- which means an
undocumented v9 table stops the daemon opening its database, not merely the
docs CLI (ADR 11 consequence 1).
"""

from __future__ import annotations

ENROLLMENT_TABLE_DOCS: dict[str, str] = {
    "machine_owners": (
        "Which user owns which machine, one live row per machine. "
        "Hub-authoritative and deliberately OUTSIDE the sync set "
        "(specs/design_decision_12.md section A): ownership is asserted only "
        "by a call that carried a live credential to the hub holding the row, "
        "so a restored or hostile spoke cannot push itself an owner under "
        "last-writer-wins. Separate from machines rather than a column on it "
        "for three reasons -- no wire payload changes shape, a revoked owner "
        "stays distinguishable from a machine that was never claimed, and the "
        "machines snapshot that peers exchange can no longer carry a forged "
        "ownership claim."
    ),
    "enrollment_grants": (
        "Short-lived single-use credentials for the DEV enrollment path: an "
        "operator with an authenticated session on the hub mints one, carries "
        "it to the machine that is joining, and that machine spends it at "
        "POST /api/v1/sync/enroll. Only the sha256 is stored, never the "
        "redeemable value, on the same reasoning as "
        "auth_sessions.session_token_sha256."
    ),
}

ENROLLMENT_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "machine_owners": {
        "machine_id": (
            "Primary key and FK to machines. One owner per machine: a shared "
            "or household machine needs a further migration, which this table "
            "makes a primary-key change rather than a redesign."
        ),
        "google_sub": (
            "FK to users. Google's 'sub' claim, the only identifier Google "
            "guarantees is stable and never reused -- email is not the key "
            "because a Google account's email can change. ON DELETE CASCADE, "
            "so deleting an account (the ACCT-03 privacy promise) un-owns its "
            "fleet and the machines survive reading unowned."
        ),
        "hub_machine_id": (
            "The machine id of the hub that WROTE this row. Honored only when "
            "it equals this hub's live id read from <data-dir>/machine-id; a "
            "row that fails that test is reported as FOREIGN and the machine "
            "reads unowned. That is what stops a hub restored from another "
            "machine's backup silently adopting that machine's whole fleet, "
            "since the DB travels in a restore but the id file does not."
        ),
        "enrolled_at": (
            "When this machine joined, canonical UTC. Never rewritten by a "
            "re-run: enrolling an already-enrolled machine is a no-op, so this "
            "column stays the audit trail of when the machine ACTUALLY joined."
        ),
        "enrolled_via": (
            "How the owner was established: 'grant' (the dev path, a "
            "single-use token carried by a human), 'google_id_token' (the "
            "zero-ceremony user path, an install proving its signed-in user to "
            "the hub), or 'adopt' (claimed retroactively by an operator, for a "
            "machine that was already syncing before ownership existed). "
            "CHECK-constrained; the vocabulary lives in "
            "apps.shared.state.migrations_v9.ENROLLED_VIA_VALUES."
        ),
        "revoked_at": (
            "When the owner released this machine, NULL while the row is live. "
            "A separate column rather than deleting the row so 'released' stays "
            "distinguishable from 'never claimed' -- the two want different "
            "handling and different messages."
        ),
    },
    "enrollment_grants": {
        "grant_token_sha256": (
            "Primary key: sha256 of the raw grant, which is the only form that "
            "reaches the database. The redeemable value is returned exactly "
            "once, to the operator who minted it."
        ),
        "google_sub": (
            "FK to users: who this grant will make the owner. Read from THIS "
            "row at redemption time and never from the request body, so an "
            "enrolling machine cannot name whose machine it is becoming."
        ),
        "created_at": "When the grant was minted, canonical UTC.",
        "expires_at": (
            "When the grant stops being redeemable, canonical UTC. Default TTL "
            "is 15 minutes (apps.sync_hub.enrollment_credentials.GRANT_TTL_S)."
        ),
        "redeemed_at": (
            "When the grant was spent, NULL while unspent. Single use: a "
            "redeemed grant presented by a DIFFERENT machine is refused. "
            "Re-presenting it for the machine that already spent it is allowed "
            "and resolves to the same owner, which is what makes the enroll "
            "command safely re-runnable."
        ),
        "redeemed_machine_id": (
            "Which machine spent this grant. Not a foreign key: it is an audit "
            "record of what happened, and it must survive the machine row being "
            "removed."
        ),
    },
}

__all__ = ["ENROLLMENT_COLUMN_DOCS", "ENROLLMENT_TABLE_DOCS"]
