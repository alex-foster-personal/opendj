"""Claim-before-repair lock for main-red trunk work (10xHunter X26).

Several agents opened parallel PRs for the same failing identity. One owner
holds the repair lock per main head and identity set until release or lease expiry.

Coordination:
- Host-local file + lock under ``OPENDJ_CI_WATCH_DIR`` (default: the same tree as
  ``scripts.ci_main_red`` uses for ``main-red.json``).
- Cross-host: when ``gh`` can reach GitHub, claim/release also posts to the open
  ``ci:trunk-red`` issue (override with ``OPENDJ_MAIN_RED_REPAIR_ISSUE``), matching
  the job-boards anti-collision surface documented in ``(private path)``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, cast

from apps.shared import file_lock
from scripts.ci_main_red import CACHE_PATH as MAIN_RED_CACHE
from scripts.ci_main_red import MainRed, cached_main_red
from scripts.gh_version_guard import check_min_version, parse_gh_version

DEFAULT_CLAIM_DIR = MAIN_RED_CACHE.parent
LEASE_S = 3 * 3600.0
LINE_PREFIX = "[main-red-repair-claim]"

EXIT_HELD = 1
EXIT_PRECONDITION = 2
EXIT_ERROR = 3

GITHUB_CLAIM_MARKER = "<!-- main-red-repair-claim v1 -->"
GITHUB_RELEASE_MARKER = "<!-- main-red-repair-release v1 -->"
DEFAULT_REPO = "private_owner/music-dj-tools"

_GH_RUN: Callable[[list[str]], str] | None = None
_CLI_CLAIM_DIR: Path | None = None


class GithubCoordinationError(RuntimeError):
    """GitHub-backed coordination was required but could not complete."""


class UnsupportedCoordModeError(ValueError):
    """``OPENDJ_MAIN_RED_REPAIR_COORD`` is not a supported mode."""


@dataclass(frozen=True)
class ClaimRecord:
    main_sha: str
    identities_key: str
    owner: str
    claimed_at_unix: float
    identities: tuple[str, ...]


def set_gh_runner(run: Callable[[list[str]], str] | None) -> None:
    """Test seam: replace ``gh`` subprocess calls (production passes ``None``)."""
    global _GH_RUN
    _GH_RUN = run


def reset_cli_claim_dir() -> None:
    """Test seam: clear a one-shot ``--claim-dir`` override."""
    global _CLI_CLAIM_DIR
    _CLI_CLAIM_DIR = None


def resolve_claim_dir(claim_dir: Path | None = None) -> Path:
    if claim_dir is not None:
        return claim_dir.expanduser()
    if _CLI_CLAIM_DIR is not None:
        return _CLI_CLAIM_DIR
    env = os.environ.get("OPENDJ_CI_WATCH_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return DEFAULT_CLAIM_DIR


def claim_file_paths(claim_dir: Path | None = None) -> tuple[Path, Path]:
    base = resolve_claim_dir(claim_dir)
    return base / "main-red-repair-claim.json", base / "main-red-repair-claim.lock"


def identities_key(identities: frozenset[str]) -> str:
    payload = "\n".join(sorted(identities)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _main_is_red(red: MainRed) -> bool:
    return bool(red.identities or red.failed_job_names or red.unreadable_job_names)


def _claim_label_tokens(red: MainRed) -> frozenset[str]:
    tokens: set[str] = set()
    for identity in red.identities:
        tokens.add(f"id:{identity}")
    all_jobs = red.failed_job_names | red.unreadable_job_names
    for job in all_jobs:
        tokens.add(f"jobname:{job}")
    for job in red.failed_job_names:
        tokens.add(f"job:{job}")
    for job in red.unreadable_job_names:
        tokens.add(f"unreadable:{job}")
    return frozenset(tokens)


def claim_key(red: MainRed) -> str:
    """Stable lock key for the current main-red baseline (all failure categories)."""
    tokens = _claim_label_tokens(red)
    payload = "\n".join(sorted(tokens)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _claim_labels(red: MainRed) -> tuple[str, ...]:
    return tuple(sorted(_claim_label_tokens(red)))


def _claim_overlap(existing: ClaimRecord, baseline: MainRed) -> frozenset[str]:
    return frozenset(existing.identities) & frozenset(_claim_labels(baseline))


def _claims_conflict(existing: ClaimRecord, baseline: MainRed) -> bool:
    if _claim_overlap(existing, baseline):
        return True
    return existing.identities_key == claim_key(baseline)


@lru_cache(maxsize=1)
def _gh_executable() -> str:
    gh = shutil.which("gh")
    if gh is None:
        raise GithubCoordinationError("gh is not on PATH")
    return gh


def _require_gh_ready() -> None:
    if _GH_RUN is not None:
        return
    gh = _gh_executable()
    proc = subprocess.run([gh, "--version"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GithubCoordinationError(
            f"`gh --version` failed ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip() or '<no output>'}"
        )
    check_min_version(parse_gh_version(proc.stdout))


def _gh_json(args: list[str]) -> str:
    if _GH_RUN is not None:
        return _GH_RUN(args)
    _require_gh_ready()
    return subprocess.check_output([_gh_executable(), *args], text=True)


def _github_repo() -> str:
    return os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO).strip() or DEFAULT_REPO


def _gh_auth_reachable() -> bool:
    if os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        return True
    try:
        gh = _gh_executable()
    except GithubCoordinationError:
        return False
    try:
        subprocess.run(
            [gh, "auth", "status"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def reset_gh_executable_cache() -> None:
    """Test seam: PATH changes after import must re-resolve ``gh``."""
    _gh_executable.cache_clear()


def _coord_mode() -> str:
    raw = os.environ.get("OPENDJ_MAIN_RED_REPAIR_COORD", "auto").strip().lower()
    if raw in {"file", "github", "both"}:
        return raw
    if raw == "auto":
        if _gh_auth_reachable():
            return "both"
        return "file"
    raise UnsupportedCoordModeError(
        f"unsupported OPENDJ_MAIN_RED_REPAIR_COORD={raw!r} "
        "(expected auto, file, github, or both)"
    )


def _github_coord_required() -> bool:
    return _coord_mode() in {"github", "both"}


def _resolve_github_issue_number() -> int | None:
    explicit = os.environ.get("OPENDJ_MAIN_RED_REPAIR_ISSUE", "").strip()
    if explicit:
        return int(explicit)
    if _coord_mode() == "file":
        return None
    try:
        raw = _gh_json(
            [
                "issue",
                "list",
                "--repo",
                _github_repo(),
                "--label",
                "ci:trunk-red",
                "--state",
                "open",
                "--limit",
                "1",
                "--json",
                "number",
            ]
        )
        items = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, ValueError) as exc:
        if _github_coord_required():
            raise GithubCoordinationError(f"failed to list ci:trunk-red issues: {exc}") from exc
        return None
    if not items:
        if _github_coord_required():
            raise GithubCoordinationError("no open issue with label ci:trunk-red")
        return None
    return int(items[0]["number"])


def _require_github_issue() -> int:
    issue = _resolve_github_issue_number()
    if issue is None:
        raise GithubCoordinationError(
            "GitHub coordination required but no ci:trunk-red issue is available"
        )
    return issue


def _record_from_payload(raw: dict[str, object]) -> ClaimRecord:
    claimed_at = raw["claimed_at_unix"]
    identities_raw = raw["identities"]
    return ClaimRecord(
        main_sha=str(raw["main_sha"]),
        identities_key=str(raw["identities_key"]),
        owner=str(raw["owner"]),
        claimed_at_unix=float(claimed_at) if isinstance(claimed_at, (int, float)) else float(str(claimed_at)),
        identities=tuple(str(x) for x in cast(list[object], identities_raw)),
    )


def _record_to_payload(record: ClaimRecord) -> dict[str, object]:
    return asdict(record)


def _flatten_github_pages(pages: object) -> list[dict[str, object]]:
    if not isinstance(pages, list):
        return []
    if not pages:
        return []
    if isinstance(pages[0], list):
        return [item for page in pages for item in page]  # type: ignore[misc]
    return pages  # type: ignore[return-value]


def _comment_sort_key(comment: dict[str, object]) -> tuple[str, int]:
    created = str(comment.get("created_at", ""))
    raw_id = comment.get("id")
    if isinstance(raw_id, int):
        comment_id = raw_id
    elif isinstance(raw_id, str):
        try:
            comment_id = int(raw_id)
        except ValueError:
            comment_id = 0
    else:
        comment_id = 0
    return created, comment_id


def _claim_slot_key(record: ClaimRecord) -> tuple[str, str]:
    return record.owner, record.identities_key


def parse_github_active_claims(comments: list[dict[str, object]]) -> list[tuple[int, ClaimRecord]]:
    """Return every live claim (comment id, record), one per owner and identities_key."""
    active: dict[tuple[str, str], tuple[int, ClaimRecord]] = {}
    for comment in sorted(comments, key=_comment_sort_key):
        body = str(comment.get("body") or "")
        comment_id = _comment_sort_key(comment)[1]
        if GITHUB_RELEASE_MARKER in body:
            owner_m = re.search(r"owner=([^\s]+)", body)
            if owner_m:
                released_owner = owner_m.group(1)
                active = {slot: pair for slot, pair in active.items() if slot[0] != released_owner}
            continue
        if GITHUB_CLAIM_MARKER not in body:
            continue
        payload_line = body.split(GITHUB_CLAIM_MARKER, 1)[-1].strip().splitlines()[0]
        try:
            record = _record_from_payload(json.loads(payload_line))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        active[_claim_slot_key(record)] = (comment_id, record)
    return list(active.values())


def parse_github_claim_thread(comments: list[dict[str, object]]) -> ClaimRecord | None:
    """Earliest comment id among active claims (legacy single-winner view)."""
    active = parse_github_active_claims(comments)
    if not active:
        return None
    _, winner = min(active, key=lambda pair: pair[0])
    return winner


def _load_github_comments(issue: int) -> list[dict[str, object]]:
    owner, repo = _github_repo().split("/", 1)
    try:
        raw = _gh_json(
            ["api", f"repos/{owner}/{repo}/issues/{issue}/comments", "--paginate", "--slurp"]
        )
        pages = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise GithubCoordinationError(f"failed to load issue #{issue} comments: {exc}") from exc
    return _flatten_github_pages(pages)


def _load_github_active_claims(issue: int) -> list[tuple[int, ClaimRecord]]:
    return parse_github_active_claims(_load_github_comments(issue))


def _load_active_remote_claims(now: float) -> list[ClaimRecord]:
    if _coord_mode() == "file":
        return []
    issue = _require_github_issue()
    active = _load_github_active_claims(issue)
    return [record for _cid, record in active if not _lease_expired(record, now)]


def _post_github_claim(issue: int, record: ClaimRecord) -> None:
    owner, repo = _github_repo().split("/", 1)
    body = f"{GITHUB_CLAIM_MARKER}\n{json.dumps(_record_to_payload(record), sort_keys=True)}"
    try:
        _gh_json(
            [
                "api",
                "-X",
                "POST",
                f"repos/{owner}/{repo}/issues/{issue}/comments",
                "-f",
                f"body={body}",
            ]
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GithubCoordinationError(f"failed to post GitHub claim: {exc}") from exc


def _post_github_release(issue: int, owner: str) -> None:
    owner_name, repo = _github_repo().split("/", 1)
    body = f"{GITHUB_RELEASE_MARKER} owner={owner}"
    try:
        _gh_json(
            [
                "api",
                "-X",
                "POST",
                f"repos/{owner_name}/{repo}/issues/{issue}/comments",
                "-f",
                f"body={body}",
            ]
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GithubCoordinationError(f"failed to post GitHub release: {exc}") from exc


@contextlib.contextmanager
def _claim_lock(claim_dir: Path | None = None):
    _, lock_path = claim_file_paths(claim_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    file_lock.lock_exclusive(fd)
    try:
        yield
    finally:
        file_lock.unlock(fd)
        os.close(fd)


_FILE_CLAIMS_VERSION = 2


def _load_file_claims(claim_dir: Path | None = None) -> list[ClaimRecord]:
    claim_file, _ = claim_file_paths(claim_dir)
    if not claim_file.is_file():
        return []
    raw = json.loads(claim_file.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("version") == _FILE_CLAIMS_VERSION:
        claims_raw = raw.get("claims")
        if not isinstance(claims_raw, list):
            raise ValueError(f"corrupt claim file: {claim_file}")
        return [_record_from_payload(item) for item in claims_raw]
    if isinstance(raw, dict) and "owner" in raw:
        return [_record_from_payload(raw)]
    raise ValueError(f"corrupt claim file: {claim_file}")


def _save_file_claims(claims: list[ClaimRecord], claim_dir: Path | None = None) -> None:
    claim_file, _ = claim_file_paths(claim_dir)
    if not claims:
        claim_file.unlink(missing_ok=True)
        return
    payload = {
        "version": _FILE_CLAIMS_VERSION,
        "claims": [_record_to_payload(record) for record in claims],
    }
    tmp = claim_file.with_suffix(f".tmp.{time.time_ns()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(claim_file)


def _upsert_file_claim(record: ClaimRecord, claim_dir: Path | None = None) -> None:
    """Persist ``record`` for its owner/failure slot while preserving all other claims."""
    slot = _claim_slot_key(record)
    kept = [c for c in _load_file_claims(claim_dir) if _claim_slot_key(c) != slot]
    kept.append(record)
    _save_file_claims(kept, claim_dir)


def _remove_file_claims_for_owner(owner: str, claim_dir: Path | None = None) -> bool:
    claims = _load_file_claims(claim_dir)
    remaining = [c for c in claims if c.owner != owner]
    had = len(remaining) < len(claims)
    _save_file_claims(remaining, claim_dir)
    return had


def _clear_file(claim_dir: Path | None = None) -> None:
    claim_file, _ = claim_file_paths(claim_dir)
    claim_file.unlink(missing_ok=True)


def _primary_file_claim(
    claims: list[ClaimRecord], baseline: MainRed, ts: float
) -> ClaimRecord | None:
    if not claims:
        return None
    key = claim_key(baseline)
    live = [c for c in claims if not _lease_expired(c, ts)]
    for record in live:
        if _claims_conflict(record, baseline):
            return record
    for record in live:
        if record.identities_key == key:
            return record
    if live:
        return None
    for record in claims:
        if record.identities_key == key or _claims_conflict(record, baseline):
            return record
    return claims[0]


def _current_red() -> MainRed:
    return cached_main_red(cache_path=MAIN_RED_CACHE)


def _lease_expired(record: ClaimRecord, now: float) -> bool:
    return now - record.claimed_at_unix >= LEASE_S


def _matches_current(record: ClaimRecord, red: MainRed, now: float) -> bool:
    key = claim_key(red)
    return record.main_sha == red.main_sha and record.identities_key == key and not _lease_expired(
        record, now
    )


def _sync_github_claim(record: ClaimRecord) -> None:
    mode = _coord_mode()
    if mode not in {"github", "both"}:
        return
    issue = _require_github_issue()
    _post_github_claim(issue, record)


def _sync_github_release(owner: str) -> None:
    mode = _coord_mode()
    if mode not in {"github", "both"}:
        return
    issue = _require_github_issue()
    _post_github_release(issue, owner)


def _reconcile_github_claim_after_sync(
    owner: str,
    baseline: MainRed,
    *,
    claim_dir: Path | None,
    ts: float,
) -> tuple[bool, str]:
    """After posting a claim, re-read GitHub and yield if another host won the race."""
    mode = _coord_mode()
    if mode == "file":
        return True, ""
    issue = _require_github_issue()
    active = _load_github_active_claims(issue)
    overlapping = [
        (comment_id, record)
        for comment_id, record in active
        if not _lease_expired(record, ts) and _claims_conflict(record, baseline)
    ]
    if not overlapping:
        return True, ""
    winner_id, winner = min(overlapping, key=lambda pair: pair[0])
    if winner.owner == owner:
        return True, ""
    overlap = _claim_overlap(winner, baseline)
    with _claim_lock(claim_dir):
        if any(c.owner == owner for c in _load_file_claims(claim_dir)):
            _remove_file_claims_for_owner(owner, claim_dir)
    _post_github_release(issue, owner)
    return (
        False,
        f"{LINE_PREFIX} held by {winner.owner} on GitHub for overlapping failures "
        f"({len(overlap)} shared); main {baseline.main_sha[:8]} identities_key={claim_key(baseline)}",
    )


def _check_remote_conflict(
    existing: ClaimRecord,
    owner: str,
    baseline: MainRed,
    now: float,
) -> tuple[bool, str]:
    if _lease_expired(existing, now):
        return True, ""
    if not _claims_conflict(existing, baseline):
        return True, ""
    if existing.owner == owner:
        return True, ""
    overlap = _claim_overlap(existing, baseline)
    return (
        False,
        f"{LINE_PREFIX} held by {existing.owner} on GitHub for overlapping failures "
        f"({len(overlap)} shared); main {baseline.main_sha[:8]} identities_key={claim_key(baseline)}",
    )


def try_claim(
    owner: str,
    *,
    takeover_note: str = "",
    now: float | None = None,
    red: MainRed | None = None,
    claim_dir: Path | None = None,
) -> tuple[bool, str]:
    """Return (True, message) when ``owner`` holds the lock, else (False, reason)."""
    ts = time.time() if now is None else now
    baseline = red if red is not None else _current_red()
    if not _main_is_red(baseline):
        return False, f"{LINE_PREFIX} main is not red; nothing to claim"
    key = claim_key(baseline)
    labels = _claim_labels(baseline)

    for remote in _load_active_remote_claims(ts):
        ok_remote, remote_msg = _check_remote_conflict(remote, owner, baseline, ts)
        if not ok_remote:
            return False, remote_msg

    github_sync: ClaimRecord | None = None
    with _claim_lock(claim_dir):
        all_claims = _load_file_claims(claim_dir)
        for live in (c for c in all_claims if not _lease_expired(c, ts)):
            if _claims_conflict(live, baseline) and live.owner != owner:
                overlap = _claim_overlap(live, baseline)
                return (
                    False,
                    f"{LINE_PREFIX} held by {live.owner} since {live.claimed_at_unix:.0f}; "
                    f"overlapping failures ({len(overlap)})",
                )
        existing = _primary_file_claim(all_claims, baseline, ts)
        if existing and not _lease_expired(existing, ts):
            overlap = _claim_overlap(existing, baseline)
            if overlap and existing.owner != owner:
                return (
                    False,
                    f"{LINE_PREFIX} held by {existing.owner} since {existing.claimed_at_unix:.0f}; "
                    f"overlapping failures ({len(overlap)})",
                )
            if existing.owner == owner and overlap:
                record = ClaimRecord(
                    main_sha=baseline.main_sha,
                    identities_key=key,
                    owner=owner,
                    claimed_at_unix=existing.claimed_at_unix,
                    identities=labels,
                )
                _upsert_file_claim(record, claim_dir)
                github_sync = record
                msg = (
                    f"{LINE_PREFIX} already held by {owner} at main {baseline.main_sha[:8]} "
                    f"(refreshed baseline)"
                )
            elif _matches_current(existing, baseline, ts):
                if existing.owner == owner:
                    return True, f"{LINE_PREFIX} already held by {owner} at main {baseline.main_sha[:8]}"
                return (
                    False,
                    f"{LINE_PREFIX} held by {existing.owner} since {existing.claimed_at_unix:.0f}; "
                    f"main {baseline.main_sha[:8]} identities_key={key}",
                )
            elif existing.main_sha == baseline.main_sha and existing.identities_key == key:
                return (
                    False,
                    f"{LINE_PREFIX} held by {existing.owner}; lease has not expired",
                )
            else:
                record = ClaimRecord(
                    main_sha=baseline.main_sha,
                    identities_key=key,
                    owner=owner,
                    claimed_at_unix=ts,
                    identities=labels,
                )
                _upsert_file_claim(record, claim_dir)
                github_sync = record
                msg = _format_claimed_message(owner, baseline, key, takeover_note)
        elif existing and _matches_current(existing, baseline, ts):
            if existing.owner == owner:
                return True, f"{LINE_PREFIX} already held by {owner} at main {baseline.main_sha[:8]}"
            return (
                False,
                f"{LINE_PREFIX} held by {existing.owner} since {existing.claimed_at_unix:.0f}; "
                f"main {baseline.main_sha[:8]} identities_key={key}",
            )
        elif existing and existing.main_sha == baseline.main_sha and existing.identities_key == key:
            if not _lease_expired(existing, ts):
                return (
                    False,
                    f"{LINE_PREFIX} held by {existing.owner}; lease has not expired",
                )
            if not takeover_note.strip():
                return (
                    False,
                    f"{LINE_PREFIX} lease expired but --takeover-note is required to take over "
                    f"from {existing.owner}",
                )
            record = ClaimRecord(
                main_sha=baseline.main_sha,
                identities_key=key,
                owner=owner,
                claimed_at_unix=ts,
                identities=labels,
            )
            _upsert_file_claim(record, claim_dir)
            github_sync = record
            msg = _format_claimed_message(owner, baseline, key, takeover_note)
        else:
            record = ClaimRecord(
                main_sha=baseline.main_sha,
                identities_key=key,
                owner=owner,
                claimed_at_unix=ts,
                identities=labels,
            )
            _upsert_file_claim(record, claim_dir)
            github_sync = record
            msg = _format_claimed_message(owner, baseline, key, takeover_note)
    if github_sync is not None:
        _sync_github_claim(github_sync)
        ok_after, after_msg = _reconcile_github_claim_after_sync(
            owner, baseline, claim_dir=claim_dir, ts=ts
        )
        if not ok_after:
            return False, after_msg
    return True, msg


def _format_claimed_message(
    owner: str, baseline: MainRed, key: str, takeover_note: str
) -> str:
    note = f" takeover={takeover_note!r}" if takeover_note.strip() else ""
    n = len(baseline.identities) or len(baseline.failed_job_names | baseline.unreadable_job_names)
    return (
        f"{LINE_PREFIX} claimed by {owner} at main {baseline.main_sha[:8]} "
        f"identities_key={key} n={n}{note}"
    )


def release(owner: str, *, claim_dir: Path | None = None) -> tuple[bool, str]:
    had_local = False
    local_other_owners: list[str] = []
    with _claim_lock(claim_dir):
        for existing in _load_file_claims(claim_dir):
            if existing.owner != owner:
                local_other_owners.append(existing.owner)
        had_local = _remove_file_claims_for_owner(owner, claim_dir)

    mode = _coord_mode()
    if mode in {"github", "both"}:
        issue = _require_github_issue()
        remote_claims = _load_github_active_claims(issue)
        now = time.time()
        remote = next(
            (record for _cid, record in remote_claims if record.owner == owner and not _lease_expired(record, now)),
            None,
        )
        if remote is not None:
            _sync_github_release(owner)
            if local_other_owners:
                others = ", ".join(sorted(set(local_other_owners)))
                return (
                    True,
                    f"{LINE_PREFIX} released remote claim for {owner}; "
                    f"local file held by {others} (unchanged)",
                )
            return True, f"{LINE_PREFIX} released by {owner}"

    if had_local:
        _sync_github_release(owner)
        return True, f"{LINE_PREFIX} released by {owner}"
    if local_other_owners:
        others = ", ".join(sorted(set(local_other_owners)))
        return False, f"{LINE_PREFIX} held by {others}, not {owner}"
    return True, f"{LINE_PREFIX} no active claim"


def status(*, claim_dir: Path | None = None) -> str:
    red = _current_red()
    claims = _load_file_claims(claim_dir)
    lines = [
        f"{LINE_PREFIX} main_sha={red.main_sha} identities={len(red.identities)} "
        f"identities_key={claim_key(red)} red={_main_is_red(red)} "
        f"coord={_coord_mode()}",
    ]
    if not claims:
        lines.append(f"{LINE_PREFIX} claim=none")
        return "\n".join(lines)
    now = time.time()
    for existing in claims:
        lines.append(
            f"{LINE_PREFIX} claim owner={existing.owner} main_sha={existing.main_sha} "
            f"identities_key={existing.identities_key} "
            f"age_s={now - existing.claimed_at_unix:.0f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        return _main_inner(argv)
    except (GithubCoordinationError, UnsupportedCoordModeError) as exc:
        print(f"{LINE_PREFIX} error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: distinct code from EXIT_HELD
        print(f"{LINE_PREFIX} error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _main_inner(argv: list[str] | None) -> int:
    global _CLI_CLAIM_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--claim-dir",
        type=Path,
        default=None,
        help="override OPENDJ_CI_WATCH_DIR for this invocation",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    try_p = sub.add_parser("try", help="claim the current main-red set for one owner")
    try_p.add_argument("--owner", required=True)
    try_p.add_argument("--takeover-note", default="")
    rel_p = sub.add_parser("release", help="release a claim held by owner")
    rel_p.add_argument("--owner", required=True)
    sub.add_parser("status", help="print the current claim and main-red summary")
    args = parser.parse_args(argv)
    _CLI_CLAIM_DIR = args.claim_dir
    # Fail on invalid OPENDJ_MAIN_RED_REPAIR_COORD before main-red or GitHub I/O.
    _coord_mode()

    if args.cmd == "try":
        ok, message = try_claim(args.owner, takeover_note=args.takeover_note, claim_dir=args.claim_dir)
        print(message)
        if not ok and "not red" in message:
            return EXIT_PRECONDITION
        return 0 if ok else EXIT_HELD
    if args.cmd == "release":
        ok, message = release(args.owner, claim_dir=args.claim_dir)
        print(message)
        return 0 if ok else EXIT_HELD
    print(status(claim_dir=args.claim_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
