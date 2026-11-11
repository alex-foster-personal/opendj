"""Blocking, process-local gate notifications; see (private path).

A wake requests an existing gate re-check, never certifies a passing gate.
The caller supplies trusted normalized updates and owns durable delivery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Iterator
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Literal
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
Kind = Literal["ci.completed", "review.posted", "queue.result"]
KINDS = frozenset({"ci.completed", "review.posted", "queue.result"})
PENDING_STATES = {
    "ci.completed": frozenset({"queued", "in_progress"}),
    "review.posted": frozenset({"queued", "in_progress"}),
    "queue.result": frozenset({"queued", "testing"}),
}
TERMINAL_STATES = {
    "ci.completed": frozenset(
        {
            "success",
            "failure",
            "cancelled",
            "timed_out",
            "neutral",
            "skipped",
            "action_required",
            "stale",
            "startup_failure",
        }
    ),
    "review.posted": frozenset({"posted"}),
    "queue.result": frozenset({"merged", "failed", "cancelled", "removed"}),
}
Identity = tuple[str, int, str, str, str, str]


def _validate_identity(repo: str, pr: int, head_sha: str) -> None:
    if (
        not isinstance(repo, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo)
        or repo.rsplit("/", maxsplit=1)[-1] in {".", ".."}
    ):
        raise ValueError("repo must be OWNER/REPO")
    if type(pr) is not int or pr <= 0:
        raise ValueError("pr must be a positive integer")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        raise ValueError("head_sha must be a full 40-character hexadecimal SHA")


def _validate_text(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{field} must be nonempty text without surrounding whitespace or controls")


@dataclass(frozen=True)
class WatchSpec:
    repo: str
    pr: int
    head_sha: str
    kinds: frozenset[Kind]

    def __post_init__(self) -> None:
        _validate_identity(self.repo, self.pr, self.head_sha)
        if not isinstance(self.kinds, frozenset) or not self.kinds or not self.kinds <= KINDS:
            raise ValueError("kinds must be a nonempty frozenset of supported wake kinds")
        object.__setattr__(self, "repo", self.repo.casefold())
        object.__setattr__(self, "head_sha", self.head_sha.lower())


@dataclass(frozen=True)
class GateUpdate:
    schema_version: int
    repo: str
    pr: int
    head_sha: str
    kind: Kind
    source_id: str
    change_id: str
    state: str
    url: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("schema_version must be 1")
        _validate_identity(self.repo, self.pr, self.head_sha)
        if not isinstance(self.kind, str) or self.kind not in KINDS:
            raise ValueError("unsupported kind")
        if not isinstance(self.state, str) or self.state not in (
            PENDING_STATES[self.kind] | TERMINAL_STATES[self.kind]
        ):
            raise ValueError("unsupported state for kind")
        _validate_text(self.source_id, "source_id")
        _validate_text(self.change_id, "change_id")
        _validate_text(self.url, "url")
        url = urlsplit(self.url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("url must be an absolute HTTP(S) evidence URL without credentials")
        object.__setattr__(self, "repo", self.repo.casefold())
        object.__setattr__(self, "head_sha", self.head_sha.lower())


@dataclass(frozen=True)
class WakeEvent(GateUpdate):
    event_id: str


def _identity(update: GateUpdate) -> Identity:
    return (update.repo, update.pr, update.head_sha, update.kind, update.source_id, update.change_id)


class WakeWatcher:
    """Filter one pinned watch; remember terminal revisions for this lifetime."""

    def __init__(self, watch_spec: WatchSpec) -> None:
        self.watch_spec = watch_spec
        self._seen: dict[Identity, str] = {}

    def observe(self, update: GateUpdate) -> WakeEvent | None:
        watch = self.watch_spec
        if (update.repo, update.pr, update.head_sha) != (watch.repo, watch.pr, watch.head_sha):
            return None
        if update.kind not in watch.kinds or update.state in PENDING_STATES[update.kind]:
            return None
        identity = _identity(update)
        if identity in self._seen:
            if self._seen[identity] != update.state:
                raise ValueError("conflicting state for a previously delivered semantic revision")
            return None
        self._seen[identity] = update.state
        encoded = json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        event_id = "sha256:" + hashlib.sha256(encoded).hexdigest()
        return WakeEvent(**asdict(update), event_id=event_id)


def iter_wakeups(updates: Iterable[GateUpdate], watch_spec: WatchSpec) -> Iterator[WakeEvent]:
    """Wait by advancing the caller's blocking iterator, without polling."""
    watcher = WakeWatcher(watch_spec)
    for update in updates:
        wake = watcher.observe(update)
        if wake is not None:
            yield wake


def _decode_update(line: str) -> GateUpdate:
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise TypeError("expected a JSON object")
    return GateUpdate(**payload)


def _read_updates(lines: Iterable[str]) -> Iterator[GateUpdate]:
    for number, line in enumerate(lines, start=1):
        try:
            yield _decode_update(line)
        except (TypeError, ValueError) as exc:
            # Never echo an untrusted input line into diagnostics.
            raise ValueError(f"invalid input at line {number}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--kind", required=True, action="append", choices=sorted(KINDS))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        watch = WatchSpec(args.repo, args.pr, args.head_sha, frozenset(args.kind))
    except ValueError as exc:
        parser.error(str(exc))
    try:
        for wake in iter_wakeups(_read_updates(sys.stdin), watch):
            print(json.dumps(asdict(wake), sort_keys=True), flush=True)
            if args.once:
                return 0
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, BrokenPipeError):
            # Close even when flushing fails, so interpreter shutdown cannot
            # retry buffered output and replace our documented exit with 120.
            with suppress(BrokenPipeError):
                sys.stdout.close()
        print(f"lane-wakeup: {exc}", file=sys.stderr)
        return 3
    if args.once:
        print("lane-wakeup: input closed before a matching wake", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
