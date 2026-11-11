"""Migration 31 -> 32: in-app labelling tasks, responses, and karma ledger (issue #363, #11192).

Machine-local subjective-preference data: ``label_task`` defines a micro-task,
``label_response`` stores one answer per ``(task_id, anon_session)``, and
``contribution_ledger`` records immediate karma from the task's ``reward_karma``
via an AFTER INSERT trigger. ``user_id`` is optional (nullable FK to
``users.google_sub``); ``anon_session`` is required so labels work without
accounts. No wire bump and no cloud-credit grants in this migration.
"""

from __future__ import annotations

LABEL_RESPONSE_KARMA_TRIGGER = "trg_label_response_grant_karma"

_V32: list[str] = [
    """
    CREATE TABLE label_task (
        id TEXT PRIMARY KEY,
        experiment_id TEXT NOT NULL,
        hypothesis_ids TEXT NOT NULL CHECK (json_valid(hypothesis_ids)),
        modality TEXT NOT NULL,
        payload TEXT NOT NULL CHECK (json_valid(payload)),
        reward_karma INTEGER NOT NULL DEFAULT 0 CHECK (reward_karma >= 0),
        reward_credits REAL,
        max_wait_s INTEGER NOT NULL CHECK (max_wait_s > 0),
        batch_size INTEGER NOT NULL CHECK (batch_size >= 1),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE label_response (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES label_task(id) ON DELETE CASCADE,
        user_id TEXT REFERENCES users(google_sub) ON DELETE SET NULL,
        anon_session TEXT NOT NULL,
        ranking TEXT NOT NULL CHECK (json_valid(ranking)),
        strength REAL NOT NULL,
        cost_choice TEXT NOT NULL,
        ms_to_complete INTEGER NOT NULL CHECK (ms_to_complete >= 0),
        created_at TEXT NOT NULL,
        UNIQUE (task_id, anon_session)
    )
    """,
    """
    CREATE TABLE contribution_ledger (
        id TEXT PRIMARY KEY,
        response_id TEXT NOT NULL UNIQUE REFERENCES label_response(id) ON DELETE CASCADE,
        task_id TEXT NOT NULL REFERENCES label_task(id) ON DELETE CASCADE,
        user_id TEXT,
        anon_session TEXT NOT NULL,
        karma_delta INTEGER NOT NULL,
        granted_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_label_response_task ON label_response (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_contribution_ledger_anon ON contribution_ledger (anon_session)",
    f"""
    CREATE TRIGGER {LABEL_RESPONSE_KARMA_TRIGGER}
    AFTER INSERT ON label_response
    BEGIN
        INSERT INTO contribution_ledger (
            id,
            response_id,
            task_id,
            user_id,
            anon_session,
            karma_delta,
            granted_at
        )
        SELECT
            lower(hex(randomblob(16))),
            NEW.id,
            NEW.task_id,
            NEW.user_id,
            NEW.anon_session,
            (SELECT reward_karma FROM label_task WHERE id = NEW.task_id),
            NEW.created_at;
    END
    """,
]

__all__ = ["LABEL_RESPONSE_KARMA_TRIGGER", "_V32"]
