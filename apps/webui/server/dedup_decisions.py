"""Persisted duplicate-review decisions and apply journal.

Schema v1 is a decisions map only. Schema v2 adds an ``applies`` map. v1 files
are readable (``applies`` defaults to empty); the next successful write persists
v2. Corrupt files fail fast and are never overwritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from apps.shared import paths as dedup_paths

if os.name == "nt":
    import msvcrt
else:
    import fcntl

DECISIONS_FILE: Path = dedup_paths.DEDUP_DIR / "review-decisions.json"
_WRITE_LOCK = threading.Lock()
CURRENT_SCHEMA_VERSION = 2
ActionLiteral = Literal["merge", "keep-all", "skip"]


class InvalidDecisionStoreError(Exception):
    """The on-disk decision store is unreadable or semantically invalid."""


class DecisionRevisionConflict(Exception):
    """If-Match does not match the current decision-store bytes."""

    def __init__(self, current_revision: str, message: str) -> None:
        super().__init__(message)
        self.current_revision = current_revision
        self.message = message


class PersistedDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster_id: int
    cluster_key: str
    member_stable_ids: list[str]
    survivor: str
    action: ActionLiteral
    decided_at: str


class PersistedApplyPlaylist(BaseModel):
    model_config = ConfigDict(extra="forbid")
    playlist_id: str
    before: list[str]
    after: list[str]


class PersistedApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster_key: str
    survivor: str
    alias_stable_ids: list[str]
    applied_at: str
    playlists: list[PersistedApplyPlaylist]


class DecisionStore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1, 2]
    decisions: dict[str, PersistedDecision]
    applies: dict[str, PersistedApply] = {}


def empty_decision_store() -> DecisionStore:
    return DecisionStore(
        schema_version=CURRENT_SCHEMA_VERSION,
        decisions={},
        applies={},
    )


def encode_decision_store(store: DecisionStore) -> bytes:
    payload = json.dumps(
        store.model_dump(mode="json"),
        indent=2,
        sort_keys=True,
    )
    return f"{payload}\n".encode()


def decision_revision(payload: bytes) -> str:
    return f'"{hashlib.sha256(payload).hexdigest()}"'


def _validate_decision_store(store: DecisionStore) -> None:
    for cluster_key, decision in store.decisions.items():
        expected_members = sorted(set(decision.member_stable_ids))
        if cluster_key != decision.cluster_key:
            raise InvalidDecisionStoreError(
                f"decision key {cluster_key!r} does not match its cluster_key"
            )
        if not expected_members or expected_members != decision.member_stable_ids:
            raise InvalidDecisionStoreError(
                f"decision {cluster_key!r} has empty, duplicate, or unsorted members"
            )
        if decision.survivor not in expected_members:
            raise InvalidDecisionStoreError(
                f"decision {cluster_key!r} names a survivor outside its members"
            )
    for cluster_key, applied in store.applies.items():
        if cluster_key != applied.cluster_key:
            raise InvalidDecisionStoreError(
                f"apply key {cluster_key!r} does not match its cluster_key"
            )
        expected_aliases = sorted(set(applied.alias_stable_ids))
        if expected_aliases != applied.alias_stable_ids:
            raise InvalidDecisionStoreError(
                f"apply {cluster_key!r} has duplicate or unsorted alias ids"
            )
        if applied.survivor in expected_aliases:
            raise InvalidDecisionStoreError(
                f"apply {cluster_key!r} lists the survivor as an alias"
            )


def read_decision_snapshot() -> tuple[DecisionStore, str]:
    if not DECISIONS_FILE.is_file():
        store = empty_decision_store()
        payload = encode_decision_store(store)
        return store, decision_revision(payload)
    try:
        payload = DECISIONS_FILE.read_bytes()
    except OSError as exc:
        raise InvalidDecisionStoreError(
            f"cannot read {DECISIONS_FILE}: {exc}"
        ) from exc
    try:
        store = DecisionStore.model_validate_json(payload)
    except (ValidationError, ValueError) as exc:
        raise InvalidDecisionStoreError(
            f"{DECISIONS_FILE} is not a valid versioned decision store: {exc}"
        ) from exc
    _validate_decision_store(store)
    return store, decision_revision(payload)


def dump_decision_store_atomic(store: DecisionStore) -> str:
    persisted = DecisionStore(
        schema_version=CURRENT_SCHEMA_VERSION,
        decisions=store.decisions,
        applies=store.applies,
    )
    payload = encode_decision_store(persisted)
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_path = tempfile.mkstemp(
        dir=str(DECISIONS_FILE.parent),
        prefix=".review-decisions.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, DECISIONS_FILE)
        if os.name != "nt":
            directory_descriptor = os.open(DECISIONS_FILE.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except BaseException:
        Path(temp_path).unlink(missing_ok=True)
        raise
    return decision_revision(payload)


@contextmanager
def decision_file_lock() -> Iterator[None]:
    """Serialize the complete read-check-write cycle across processes."""
    with _WRITE_LOCK:
        DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock_path = DECISIONS_FILE.with_name(f"{DECISIONS_FILE.name}.lock")
        with lock_path.open("a+b") as lock_handle:
            if os.name == "nt":
                lock_handle.seek(0, os.SEEK_END)
                if lock_handle.tell() == 0:
                    lock_handle.write(b"\0")
                    lock_handle.flush()
                    os.fsync(lock_handle.fileno())
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    lock_handle.seek(0)
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def commit_decision_store(
    expected_revision: str,
    *,
    decision: PersistedDecision | None = None,
    apply: PersistedApply | None = None,
    drop_apply_key: str | None = None,
) -> str:
    """Locked read-modify-replace. Raises DecisionRevisionConflict on stale ETag."""
    with decision_file_lock():
        store, current_revision = read_decision_snapshot()
        if expected_revision != current_revision:
            raise DecisionRevisionConflict(
                current_revision,
                "If-Match does not match the current decision-store ETag",
            )
        decisions = dict(store.decisions)
        applies = dict(store.applies)
        if decision is not None:
            decisions[decision.cluster_key] = decision
        if apply is not None:
            applies[apply.cluster_key] = apply
        if drop_apply_key is not None:
            applies.pop(drop_apply_key, None)
        return dump_decision_store_atomic(
            DecisionStore(
                schema_version=CURRENT_SCHEMA_VERSION,
                decisions=decisions,
                applies=applies,
            )
        )


# Name kept for the multiprocessing lock test and route re-exports.
_decision_file_lock = decision_file_lock
