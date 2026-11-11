"""Column docs for v32 labelling tables (issue #363, #11192)."""

from __future__ import annotations

LABELLING_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "label_task": {
        "id": "Primary key for one labelling micro-task.",
        "experiment_id": "Experiment or agent-gate id this task unblocks.",
        "hypothesis_ids": "JSON array of hypothesis graph node ids the answer feeds.",
        "modality": "Task kind (for example stems_ab_rank, lyrics_sync_tap).",
        "payload": "JSON task body (clip ids, options, copy) the UI or agent renders.",
        "reward_karma": "Karma granted immediately when a response is stored.",
        "reward_credits": "Optional cloud-credit reward; not granted in schema v31.",
        "max_wait_s": "How long an agent gate may wait for this batch.",
        "batch_size": "Target label count before consensus (typically 3 to 5).",
        "created_at": "UTC timestamp when the task row was minted.",
    },
    "label_response": {
        "id": "Primary key for one stored answer.",
        "task_id": "The task this response answers. FK to label_task.",
        "user_id": "Optional signed-in user (users.google_sub); NULL for anon-only.",
        "anon_session": "Required session id; one response per task per session.",
        "ranking": "JSON ranking payload (for example ordered clip ids).",
        "strength": "How strongly the labeler feels about the choice (0-1 or scale).",
        "cost_choice": "Cost or price tradeoff choice (task-specific string).",
        "ms_to_complete": "Wall time from task show to submit, in milliseconds.",
        "created_at": "UTC timestamp when the response was stored.",
    },
    "contribution_ledger": {
        "id": "Primary key for one karma grant row.",
        "response_id": "The response that triggered this grant. One row per response.",
        "task_id": "Task whose reward_karma was applied.",
        "user_id": "Account id when the response had user_id; else NULL.",
        "anon_session": "Session that earned the karma, for later account linking.",
        "karma_delta": "Karma granted immediately on insert (from label_task.reward_karma).",
        "granted_at": "UTC timestamp of the grant (matches response created_at).",
    },
}

__all__ = ["LABELLING_COLUMN_DOCS"]
