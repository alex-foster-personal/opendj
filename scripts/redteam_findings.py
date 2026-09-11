"""Persist the immutable REDTEAM-03 finding schema.

Requirements:
    - [if] a pod cannot reach its assigned surface [then] it records UNAVAILABLE, never PASS
    - [if] a later pod reruns a fingerprint and SHA [then] it cannot overwrite the first verdict
    - [if] a pod runs on the REDTEAM-02 Windows surface [then] importing and recording still work
    - [if] a caller passes an untyped repro, evidence or scalar [then] it is refused before disk
    - [if] a lock holder wedges on either platform [then] the wait ends at a deadline, not never
    - [if] a write is interrupted part way [then] the previous ledger survives intact

The REDTEAM-01 trigger supplies a per-run directory. Pods write findings to
that directory's ``index.jsonl`` through :class:`FindingStore`; REDTEAM-04 can
consume the same immutable records for deduplication and issue filing.

-Codex
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

# Advisory locking, which POSIX and Windows spell differently and neither of
# which is importable on the other. Branched at module scope rather than
# imported bare, because `import fcntl` raises ModuleNotFoundError on Windows -
# and REDTEAM-02 puts one to two pods on bifrost2's Windows surface, so this
# module failing to IMPORT meant those pods could emit neither FAIL nor
# UNAVAILABLE. The identical defect was found on `scripts/provenance_state.py`
# on #708; this is the shape of that fix. Write the branch in the POSITIVE form
# (`== "win32"`), never its negation: mypy is pinned to darwin here, and only
# the positive form marks the Windows body unreachable, so a negated branch
# reports `msvcrt` as an undefined name on every macOS run.
if sys.platform == "win32":
    import msvcrt

    # Windows reports "another process holds this region" as EACCES from
    # `msvcrt.locking` with LK_NBLCK, and EDEADLOCK from the blocking variants.
    # Neither is a POSIX flock contention errno. Bound inside the branch
    # because `errno.EDEADLOCK` is a Windows-only name (macOS spells it
    # EDEADLK), so reading it at module scope would trade one platform-only
    # import for one platform-only attribute.
    _WIN_CONTENTION = frozenset({errno.EACCES, errno.EDEADLOCK})
else:
    import fcntl

    # POSIX `flock` with LOCK_NB reports contention as EWOULDBLOCK, which on
    # Linux and macOS is the same value as EAGAIN. Both names are listed
    # because POSIX does not guarantee that equality. EACCES is deliberately
    # NOT here: on flock it would mean a permission problem, which must be
    # raised rather than retried until the deadline.
    _POSIX_CONTENTION = frozenset({errno.EWOULDBLOCK, errno.EAGAIN})

# A Git object ID is 40 lowercase hex (SHA-1) or 64 (SHA-256) - never a length
# between. `{40,64}` accepted 41 through 63 too, and because a recorded verdict
# is immutable, one truncated SHA permanently mints a dedupe key REDTEAM-04 can
# never match against a real commit. Sol found it on #1259.
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FINDING_KEYS = frozenset(
    {
        "fingerprint",
        "verdict",
        "sha",
        "surface",
        "host",
        "repro",
        "evidence",
        "honest_coverage",
    }
)
_REPRODUCTION_KEYS = frozenset({"act_sequence", "trace_session"})
_EVIDENCE_KEYS = frozenset({"trace_url", "screenshot"})


class Verdict(StrEnum):
    """The REDTEAM-03 negative-result taxonomy."""

    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"


class ExistingFindingError(RuntimeError):
    """A finding key already has its immutable first verdict."""


@dataclass(frozen=True)
class Reproduction:
    """The production actions and trace session needed to replay a finding."""

    act_sequence: tuple[str, ...]
    trace_session: str

    def __post_init__(self) -> None:
        if not isinstance(self.act_sequence, tuple) or not self.act_sequence:
            raise TypeError("repro.act_sequence must be a non-empty tuple of strings")
        if not all(isinstance(action, str) and action.strip() for action in self.act_sequence):
            raise ValueError("repro.act_sequence needs one or more non-empty actions")
        if not isinstance(self.trace_session, str):
            raise TypeError("repro.trace_session must be a string")
        if not self.trace_session.strip():
            raise ValueError("repro.trace_session is required")


@dataclass(frozen=True)
class Evidence:
    """Trace and screenshot references, with an honest absent screenshot represented by null."""

    trace_url: str
    screenshot: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.trace_url, str):
            raise TypeError("evidence.trace_url must be a string")
        if not self.trace_url.strip():
            raise ValueError("evidence.trace_url is required")
        if self.screenshot is not None and not isinstance(self.screenshot, str):
            raise TypeError("evidence.screenshot must be null or a string")
        if self.screenshot is not None and not self.screenshot.strip():
            raise ValueError("evidence.screenshot must be null or a non-empty URL")


@dataclass(frozen=True)
class FindingDetails:
    """The shared factual fields for either allowed negative verdict."""

    fingerprint: str
    sha: str
    surface: str
    host: str
    repro: Reproduction
    evidence: Evidence
    honest_coverage: str

    def __post_init__(self) -> None:
        if not isinstance(self.repro, Reproduction):
            raise TypeError("repro must be a Reproduction")
        if not isinstance(self.evidence, Evidence):
            raise TypeError("evidence must be an Evidence")
        # And the scalars, one layer down from the nested objects above. Without
        # this a non-string `fingerprint` raised AttributeError from `.strip()`
        # and a non-string `sha` raised TypeError from `re.fullmatch` - both
        # accidents of implementation rather than a stated contract, and neither
        # names the field that was wrong.
        for name in ("fingerprint", "sha", "surface", "host", "honest_coverage"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        if not self.fingerprint.strip():
            raise ValueError("fingerprint is required")
        if not _SHA.fullmatch(self.sha):
            raise ValueError("sha must be a lowercase 40 to 64 character Git object ID")
        if not self.surface.startswith("area:") or not self.surface.removeprefix("area:").strip():
            raise ValueError("surface must begin with area: and include a label")
        if not self.host.strip():
            raise ValueError("host is required")
        if not self.honest_coverage.strip():
            raise ValueError("honest_coverage is required")


@dataclass(frozen=True)
class Finding:
    """One immutable red-team result for one fingerprint at one commit SHA."""

    fingerprint: str
    verdict: Verdict
    sha: str
    surface: str
    host: str
    repro: Reproduction
    evidence: Evidence
    honest_coverage: str

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, Verdict):
            raise TypeError("verdict must be FAIL or UNAVAILABLE")
        FindingDetails(
            fingerprint=self.fingerprint,
            sha=self.sha,
            surface=self.surface,
            host=self.host,
            repro=self.repro,
            evidence=self.evidence,
            honest_coverage=self.honest_coverage,
        )

    @classmethod
    def unavailable(
        cls,
        *,
        details: FindingDetails,
    ) -> Finding:
        """Construct the only valid outcome when an assigned surface is unreachable."""
        return cls(
            fingerprint=details.fingerprint,
            verdict=Verdict.UNAVAILABLE,
            sha=details.sha,
            surface=details.surface,
            host=details.host,
            repro=details.repro,
            evidence=details.evidence,
            honest_coverage=details.honest_coverage,
        )

    @classmethod
    def fail(
        cls,
        *,
        details: FindingDetails,
    ) -> Finding:
        """Construct a reached-surface failure finding."""
        return cls(
            fingerprint=details.fingerprint,
            verdict=Verdict.FAIL,
            sha=details.sha,
            surface=details.surface,
            host=details.host,
            repro=details.repro,
            evidence=details.evidence,
            honest_coverage=details.honest_coverage,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the schema's JSON-safe shape with every required field present."""
        data = asdict(self)
        data["verdict"] = self.verdict.value
        return data

    @classmethod
    def from_dict(cls, data: object) -> Finding:
        """Validate one persisted JSON object before trusting it as immutable evidence."""
        if not isinstance(data, dict):
            raise TypeError("finding record must be a JSON object")
        _require_exact_keys(data, _FINDING_KEYS, "finding")
        repro = data.get("repro")
        evidence = data.get("evidence")
        if not isinstance(repro, dict) or not isinstance(evidence, dict):
            raise TypeError("finding needs repro and evidence objects")
        _require_exact_keys(repro, _REPRODUCTION_KEYS, "repro")
        _require_exact_keys(evidence, _EVIDENCE_KEYS, "evidence")
        actions = repro.get("act_sequence")
        if not isinstance(actions, list) or not all(isinstance(action, str) for action in actions):
            raise ValueError("repro.act_sequence must be a JSON string array")
        values = ("fingerprint", "sha", "surface", "host", "honest_coverage")
        if not all(isinstance(data.get(key), str) for key in values):
            raise ValueError("finding scalar fields must be strings")
        if not isinstance(repro.get("trace_session"), str):
            raise TypeError("repro.trace_session must be a string")
        if not isinstance(evidence.get("trace_url"), str):
            raise TypeError("evidence.trace_url must be a string")
        screenshot = evidence.get("screenshot")
        if screenshot is not None and not isinstance(screenshot, str):
            raise ValueError("evidence.screenshot must be null or a string")
        try:
            verdict = Verdict(data.get("verdict"))
        except ValueError as error:
            raise ValueError("verdict must be FAIL or UNAVAILABLE") from error
        return cls(
            fingerprint=data["fingerprint"],
            verdict=verdict,
            sha=data["sha"],
            surface=data["surface"],
            host=data["host"],
            repro=Reproduction(act_sequence=tuple(actions), trace_session=repro["trace_session"]),
            evidence=Evidence(trace_url=evidence["trace_url"], screenshot=screenshot),
            honest_coverage=data["honest_coverage"],
        )


def _require_exact_keys(data: dict[object, object], allowed: frozenset[str], name: str) -> None:
    """Reject schema drift before an immutable record reaches its typed constructor."""
    actual = set(data)
    unknown = actual - allowed
    missing = allowed - actual
    if unknown:
        raise ValueError(f"unknown {name} fields: {', '.join(sorted(map(str, unknown)))}")
    if missing:
        raise ValueError(f"missing {name} fields: {', '.join(sorted(missing))}")


# Long enough that ordinary contention always serializes: every writer parses
# the whole index while holding the lock, so the hold time grows with the run.
# Bounded rather than unbounded so one stale lock cannot wedge a pod forever,
# and exceeding it RAISES a named error - a pod that reports LockUnavailable
# can be rerun, a pod that silently dropped a FAIL cannot be told from a pod
# that found nothing.
_LOCK_TIMEOUT_S = 120.0
_LOCK_POLL_S = 0.05


class LockUnavailable(RuntimeError):
    """The index lock could not be taken, as distinct from a rejected finding."""


def _lock_exclusive(lock_file: BinaryIO) -> None:
    """Acquire the shared lockfile's first byte on either supported host OS.

    BOTH branches poll a non-blocking lock against the same deadline, and both
    raise :class:`LockUnavailable` when it expires. Neither platform primitive
    gives that for free and each failed differently:

    - Windows `msvcrt.locking` with LK_LOCK retries just ten times, one second
      apart, then raises - it would DROP a valid finding whenever another pod
      held the lock for ten seconds, which grows likely as the ledger grows
      because every writer parses the whole index while holding it.
    - POSIX `flock(LOCK_EX)` blocks in the kernel forever, so one wedged holder
      stalls every reader and writer on the host with no error and no deadline.

    The first loses findings, the second loses the pod, and this module claimed
    bounded fail-fast locking while only the Windows half delivered it. Sol
    found both halves on #1259.

    The branch is written in the POSITIVE form (`== "win32"`), never its
    negation: mypy is pinned to darwin in this repo, and only the positive form
    marks the Windows body unreachable - a negated branch reports `msvcrt` as
    an undefined name on every macOS run.
    """
    if sys.platform == "win32":
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.errno not in _WIN_CONTENTION:
                    raise LockUnavailable(f"cannot lock {lock_file.name}: {error}") from error
                if time.monotonic() >= deadline:
                    raise LockUnavailable(
                        f"another pod held {lock_file.name} for {_LOCK_TIMEOUT_S:g}s"
                    ) from error
                time.sleep(_LOCK_POLL_S)
            else:
                return
    else:
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            lock_file.seek(0)
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in _POSIX_CONTENTION:
                    raise LockUnavailable(f"cannot lock {lock_file.name}: {error}") from error
                if time.monotonic() >= deadline:
                    raise LockUnavailable(
                        f"another pod held {lock_file.name} for {_LOCK_TIMEOUT_S:g}s"
                    ) from error
                time.sleep(_LOCK_POLL_S)
            else:
                return


def _unlock(lock_file: BinaryIO) -> None:
    """Release the byte-range or whole-file lock acquired by :func:`_lock_exclusive`."""
    lock_file.seek(0)
    if sys.platform == "win32":
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _fsync_dir(directory: Path) -> None:
    """Persist the RENAME, not only the bytes it points at.

    Without it a power loss can leave the new file's contents on disk and the
    directory entry still naming the old inode. POSIX-only: Windows cannot open
    a directory handle to fsync, and `os.replace` there is already journaled by
    the filesystem, so there is nothing to force.
    """
    if sys.platform == "win32":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class FindingStore:
    """A JSONL store keyed by ``(fingerprint, sha)`` whose records are never rewritten."""

    def __init__(self, index_path: Path) -> None:
        self._index_path = index_path

    def read_all(self) -> tuple[Finding, ...]:
        """Read and validate every immutable record in insertion order.

        A missing PARENT raises, the same as :meth:`record`. A mistyped,
        deleted or unmounted run directory is an operational failure, and
        returning an empty tuple for it would let REDTEAM-04 dedupe and file
        against "this run found nothing" - indistinguishable from a clean run.
        A missing INDEX inside a real directory still returns empty, because
        that is the honest state of a run before its first finding. Sol found
        the conflation on #1259.
        """
        self._require_parent()
        with self._lock_index():
            return self._read_locked()

    def record(self, finding: Finding) -> None:
        """Add a finding by REPLACING the index, never by appending in place.

        An in-place append can tear. A kill, an I/O error or a full disk part
        way through one leaves a truncated final line, and because
        :meth:`_read_records` validates EVERY line, the next read then rejects
        the whole ledger - one interrupted write loses every finding recorded
        before it, on a store whose entire purpose is that a verdict cannot be
        lost or changed. Sol found it on #1259.

        So the whole index is rewritten to a unique temp file in the same
        directory, fsynced, and moved over the target with :func:`os.replace`,
        which is atomic within a filesystem: a reader sees the old index or the
        new one, never a partial one. The directory is fsynced afterwards so
        the RENAME survives a power loss too, not just the bytes it points at.

        Rewriting the file costs a full pass per append, which sounds worse
        than it is: the duplicate check below already parses every record under
        the same lock, so this adds no complexity class the write did not have.
        """
        if not isinstance(finding, Finding):
            raise TypeError("finding must be a Finding")
        self._require_parent()
        with self._lock_index():
            records = self._read_locked()
            existing = next(
                (
                    record
                    for record in records
                    if record.fingerprint == finding.fingerprint and record.sha == finding.sha
                ),
                None,
            )
            if existing is not None:
                raise ExistingFindingError(
                    f"finding {finding.fingerprint!r} at {finding.sha} already has "
                    f"immutable verdict {existing.verdict.value}"
                )
            lines = [json.dumps(record.to_dict(), sort_keys=True) for record in records]
            lines.append(json.dumps(finding.to_dict(), sort_keys=True))
            self._replace_index("".join(f"{line}\n" for line in lines))

    def _read_locked(self) -> tuple[Finding, ...]:
        """Read the index assuming the caller already holds the lock."""
        if not self._index_path.exists():
            return ()
        return self._read_records(self._index_path.read_text(encoding="utf-8"))

    def _replace_index(self, text: str) -> None:
        """Publish a new index in one indivisible step, or leave the old one untouched.

        `mkstemp` gives a name no concurrent writer can collide with (O_EXCL),
        created in the TARGET's directory so the replace stays within one
        filesystem - `os.replace` is only atomic there. The mode is restored
        before the move because `os.replace` carries the source inode's mode to
        the destination, and mkstemp creates 0600: without this the ledger
        would turn owner-only on its first write, unreadable to any other pod
        or service on the host. Both details are the ones
        `scripts/provenance_state.py` had to learn on #708.

        On any failure the temp file is removed and the existing index is left
        exactly as it was, which is the whole point.
        """
        fd, tmp_name = tempfile.mkstemp(
            dir=self._index_path.parent, prefix=f".{self._index_path.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            mode = (
                stat.S_IMODE(self._index_path.stat().st_mode)
                if self._index_path.exists()
                else 0o644
            )
            os.chmod(tmp, mode)
            os.replace(tmp, self._index_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _fsync_dir(self._index_path.parent)

    def _require_parent(self) -> None:
        """Refuse to treat an absent run directory as a run that found nothing."""
        if not self._index_path.parent.is_dir():
            raise RuntimeError(f"finding index parent does not exist: {self._index_path.parent}")

    @contextlib.contextmanager
    def _lock_index(self) -> Iterator[None]:
        """Serialize index access with a Windows and POSIX compatible sidecar lock."""
        lock_path = self._index_path.with_suffix(self._index_path.suffix + ".lock")
        with lock_path.open("a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            _lock_exclusive(lock_file)
            try:
                yield
            finally:
                _unlock(lock_file)

    @staticmethod
    def _read_records(text: str) -> tuple[Finding, ...]:
        records: list[Finding] = []
        keys: set[tuple[str, str]] = set()
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                raise ValueError(f"finding index has an empty record at line {line_number}")
            try:
                records.append(Finding.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise ValueError(
                    f"invalid finding index record at line {line_number}: {error}"
                ) from error
            key = (records[-1].fingerprint, records[-1].sha)
            if key in keys:
                raise ValueError(f"finding index duplicates immutable key {key[0]!r} at {key[1]}")
            keys.add(key)
        return tuple(records)
