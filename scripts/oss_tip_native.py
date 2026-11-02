"""Native (Rust) backend for the going-public audit, behind ODJ_OSS_AUDIT_BACKEND.

The full-tree scan took 17-23 s, almost all of it reading ~320 MB of published blobs
through a Python pipe and running six regexes plus the login harvest over ~156 MB
of text. The kernel in scripts/native/oss_tip_audit/ does exactly that part and
nothing else. It holds NO rules: this module hands it the patterns, flags and
prefilter tokens from scripts/oss_tip_rules.py and the known-login pattern from
scripts/oss_tip_logins.py, and every acceptance decision (`_rule_accepts`, the
placeholder and mailbox lists, `is_exempt`, the login harvest) still runs here, in
the same functions the Python backend calls.

    ODJ_OSS_AUDIT_BACKEND=python   (default) never start the kernel
    ODJ_OSS_AUDIT_BACKEND=auto     use the kernel when it is built, else Python
    ODJ_OSS_AUDIT_BACKEND=native   the kernel must be built and must accept the
                                   patterns, or the run is UNKNOWN

    cargo build --release --manifest-path scripts/native/oss_tip_audit/Cargo.toml

ODJ_OSS_AUDIT_NATIVE_BIN overrides where the binary is looked for.

Fallback is per blob and always toward Python, never toward "clean". A blob goes
back to the Python scanner when the kernel cannot mirror Python's `re` on it: a
UTF-16/32 byte-order mark, bytes that are not valid UTF-8, a character the two
regex engines classify differently (the handshake below compares them on every
character actually present), a backtracking limit, or a candidate whose Python
re-match disagrees with the kernel's. A kernel that crashes, answers garbage or
cannot read git raises NativeAuditError, which the CLI prints as UNKNOWN and exits
2: a failed measurement is never a verdict.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from scripts import oss_tip_audit as audit
from scripts.oss_tip_logins import (
    BARE_WINDOW,
    HOME_RULES,
    bare_login_matches,
    is_bare_position,
    logins_in_text,
    revealed_login,
    reviewed_logins,
)
from scripts.oss_tip_logins import RULE as KNOWN_LOGIN_RULE
from scripts.oss_tip_rules import BINARY_SNIFF_BYTES, PATTERNS, RULE_PREFILTERS, _rule_accepts

BACKEND_ENV = "ODJ_OSS_AUDIT_BACKEND"
BINARY_ENV = "ODJ_OSS_AUDIT_NATIVE_BIN"
VERBOSE_ENV = "ODJ_OSS_AUDIT_VERBOSE"
BACKENDS = ("python", "auto", "native")
CRATE_DIR = Path(__file__).resolve().parent / "native" / "oss_tip_audit"
_GITLINK = "160000"
# The rules look back 256 characters for a URL authority (findings_in_text).
_PRE_WINDOW = 256
# Enough of the rest of the line for Python to re-check every candidate.
_POST_WINDOW = 1024
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


class NativeAuditError(RuntimeError):
    """The native kernel could not measure. The run is UNKNOWN, never clean."""


@dataclass(frozen=True)
class _Cand:
    pat: int
    line: int
    group0: str
    group1: str | None
    pre: str
    post: str
    eof: bool


def note(message: str) -> None:
    """A backend diagnostic on stderr, shown only on request: a default run prints the verdict alone."""
    if os.environ.get(VERBOSE_ENV) == "1":
        print(message, file=sys.stderr)


def backend_request() -> str:
    request = os.environ.get(BACKEND_ENV, "python").strip().lower()
    if request not in BACKENDS:
        raise NativeAuditError(f"{BACKEND_ENV} must be one of {', '.join(BACKENDS)}; got {request!r}")
    return request


def native_binary() -> Path | None:
    """The built kernel, or None. The CI build helper exports BINARY_ENV with the path cargo reported,
    so every build layout reaches the same file. An explicit override that does not exist is an error."""
    override = os.environ.get(BINARY_ENV)
    if override:
        path = Path(override)
        if not path.is_file():
            raise NativeAuditError(f"{BINARY_ENV}={override} is not a file")
        return path
    name = "oss-tip-audit-native.exe" if os.name == "nt" else "oss-tip-audit-native"
    target_dir = os.environ.get("CARGO_TARGET_DIR")
    path = (Path(target_dir) if target_dir else CRATE_DIR / "target") / "release" / name
    return path if path.is_file() else None


def _frame(value: str | int) -> bytes:
    if isinstance(value, int):
        return f"{value}\n".encode()
    try:
        data = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        # A POSIX checkout root can carry bytes that are not UTF-8; that is a measurement that cannot run, not a crash.
        raise NativeAuditError(f"kernel input is not UTF-8: {value[:80]!r}") from exc
    return f"{len(data)}\n".encode() + data + b"\n"


# The kernel's protocol version, sent back on every PROBE. Bump it with the Rust side's PROTOCOL.
PROTOCOL = 1

# A full-tree LOAD takes under 2 s on a CI runner. This bounds a kernel that is alive but silent, with
# room for a runner in an IO stall, so a hang reports [UNKNOWN] rather than waiting out the job cap.
REPLY_TIMEOUT_S = 300.0


class _Kernel:
    def __init__(self, binary: Path, reply_timeout_s: float = REPLY_TIMEOUT_S) -> None:
        self._killed = False
        self._reply_timeout_s = reply_timeout_s
        self._replies: queue.Queue[bytes | OSError] = queue.Queue()
        try:
            self._proc = subprocess.Popen(
                [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as exc:
            raise NativeAuditError(f"cannot start the kernel {binary}: {exc}") from exc
        threading.Thread(target=self._pump_replies, daemon=True).start()

    def _pump_replies(self) -> None:
        """Hand each stdout line to `receive`, which has a timeout; `readline` has none on any platform."""
        assert self._proc.stdout is not None
        try:
            for line in iter(self._proc.stdout.readline, b""):
                self._replies.put(line)
        except OSError as exc:
            self._replies.put(exc)
            return
        self._replies.put(b"")

    def ask(self, parts: Sequence[bytes | str | int]) -> dict:
        self.send(parts)
        return self.receive()

    def send(self, parts: Sequence[bytes | str | int]) -> None:
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(b"".join(_frame(p) if not isinstance(p, bytes) else p for p in parts))
            self._proc.stdin.flush()
        except OSError as exc:
            raise NativeAuditError(f"kernel pipe failed: {exc}; {self._stderr()}") from exc

    def receive(self) -> dict:
        try:
            line = self._replies.get(timeout=self._reply_timeout_s)
        except queue.Empty as exc:
            raise NativeAuditError(f"kernel sent no reply in {self._reply_timeout_s:g} s; {self._stderr()}") from exc
        if isinstance(line, OSError):
            raise NativeAuditError(f"kernel pipe failed: {line}; {self._stderr()}") from line
        if not line:
            raise NativeAuditError(f"kernel exited without an answer; {self._stderr()}")
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise NativeAuditError(f"kernel answered something that is not JSON: {line[:200]!r}") from exc
        # Valid JSON is not a valid answer: `{}` or `[]` parses, then fails later on indexing.
        if not isinstance(reply, dict) or not reply:
            raise NativeAuditError(f"kernel answered JSON that is not a reply object: {line[:200]!r}")
        return reply

    def _stderr(self) -> str:
        assert self._proc.stderr is not None
        self._proc.kill()
        self._killed = True
        returncode = self._proc.wait()
        err = self._proc.stderr.read()
        return f"exit {returncode}, stderr: {err.decode(errors='replace').strip()}"

    def close(self) -> None:
        """Shut the kernel down, and fail if it did not exit cleanly after its last answer."""
        if self._proc.poll() is None:
            assert self._proc.stdin is not None
            try:
                self._proc.stdin.write(b"QUIT\n")
                self._proc.stdin.close()
            except OSError as exc:
                self._proc.kill()
                self._reap()
                raise NativeAuditError(f"kernel refused the shutdown request: {exc}") from exc
        returncode = self._reap()
        # A process `_stderr` already killed has been reported with its stderr; do not mask that error.
        if returncode != 0 and not self._killed:
            raise NativeAuditError(f"kernel exited {returncode} after its last answer")

    def _reap(self) -> int:
        """Wait for the kernel. One that ignores its shutdown is killed and reported as a failure, never left running."""
        try:
            return self._proc.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            self._proc.kill()
            self._proc.wait()
            raise NativeAuditError("kernel did not exit within 30 seconds of the shutdown request") from exc


@dataclass(frozen=True)
class _Probe:
    """One single-character atom of a pattern: what the kernel runs, what Python runs."""

    src: str
    flags: int
    py_src: str
    py_flags: int


def _class_end(src: str, start: int) -> int:
    """Index of the `]` closing the class that opens at `start`."""
    i = start + 1
    if src.startswith("^", i):
        i += 1
    if src.startswith("]", i):
        i += 1
    while i < len(src) and src[i] != "]":
        i += 2 if src[i] == "\\" else 1
    return i


def _probes(pattern: re.Pattern[str]) -> list[_Probe]:
    """Every single-character atom whose meaning depends on the regex engine.

    Classes, category escapes and `.` under the pattern's own flags; every ASCII
    letter when the pattern ignores case; and `\\w` for a word boundary, which
    Python judges with the plain (unfolded) word class whatever the flags.
    """
    src, flags = pattern.pattern, int(pattern.flags)
    atoms: set[str] = set()
    boundary = False
    i = 0
    while i < len(src):
        if src[i] == "\\":
            nxt = src[i + 1 : i + 2]
            atoms |= {src[i : i + 2]} if nxt in set("wWdDsS") else set()
            boundary |= nxt in ("b", "B")
            i += 2
        elif src[i] == "[":
            end = _class_end(src, i)
            atoms.add(src[i : end + 1])
            i = end + 1
        else:
            atoms |= {"."} if src[i] == "." else set()
            i += 1
    # A scoped `(?i:...)` folds case without setting the global bit, so it is probed the same way.
    folded = bool(flags & re.IGNORECASE) or "(?i" in src
    if folded:
        atoms |= set(_LETTERS)
        flags |= int(re.IGNORECASE)
    probes = [_Probe(atom, flags, atom, flags) for atom in sorted(atoms)]
    return probes + ([_Probe(r"\w", flags, r"\w", 0)] if boundary else [])


def _handshake(kernel: _Kernel, patterns: Sequence[re.Pattern[str]]) -> tuple[list[int], list[list[int]]] | str:
    """(characters whose lowercase differs, per pattern the characters any atom differs on).

    The answer covers every character present in the loaded texts, so a blob holding
    a disagreeing character goes back to Python instead of being scanned on trust.
    A string is the kernel's refusal.
    """
    per_pattern = [_probes(p) for p in patterns]
    flat = [probe for probes in per_pattern for probe in probes]
    answer = kernel.ask([b"PROBE\n", len(flat), *[x for p in flat for x in (p.src, p.flags)]])
    if "refused" in answer:
        return str(answer["refused"])
    # A stale or overridden kernel answers another protocol; its bits would not mean what ours do.
    if type(answer.get("protocol")) is not int or answer["protocol"] != PROTOCOL:
        raise NativeAuditError(
            f"kernel speaks protocol {answer.get('protocol')!r}, this wrapper speaks {PROTOCOL}; rebuild the kernel"
        )
    for _codepoint, _lower, bits in answer["chars"]:
        # Each row's bits must line up with the atoms asked for, or the comparison below reads the wrong probes.
        if len(bits) != len(flat):
            raise NativeAuditError(f"kernel probe row does not hold {len(flat)} atoms: {str(bits)[:200]!r}")
    compiled = [re.compile(p.py_src, p.py_flags) for p in flat]
    lower_bad: list[int] = []
    bad: list[list[int]] = [[] for _ in patterns]
    for codepoint, lower, bits in answer["chars"]:
        ch = chr(codepoint)
        if ch.lower() != lower:
            lower_bad.append(codepoint)
        ours = "".join("1" if c.fullmatch(ch) else "0" for c in compiled)
        offset = 0
        for k, probes in enumerate(per_pattern):
            if ours[offset : offset + len(probes)] != bits[offset : offset + len(probes)]:
                bad[k].append(codepoint)
            offset += len(probes)
    return lower_bad, bad


def _scan(
    kernel: _Kernel, specs: Sequence[tuple[str, re.Pattern[str], Sequence[str]]], modes: list[int], pre: int
) -> dict:
    """Probe, then scan. A refusal comes back as {"refused": reason}."""
    handshake = _handshake(kernel, [pattern for _rule, pattern, _tokens in specs])
    if isinstance(handshake, str):
        return {"refused": handshake}
    lower_bad, bad = handshake
    parts: list = [b"SCAN\n", len(lower_bad), *lower_bad, len(specs)]
    for (rule, pattern, tokens), pattern_bad in zip(specs, bad, strict=True):
        parts += [rule, pattern.pattern, int(pattern.flags), len(tokens), *tokens, len(pattern_bad), *pattern_bad]
    parts += [len(modes), *modes, pre, _POST_WINDOW]
    return kernel.ask(parts)


def _verified(pattern: re.Pattern[str], cand: _Cand) -> re.Match[str] | None:
    """Python's own match at the kernel's position, if it is the same match."""
    # Past a truncated or line-ended window the text goes on; a NUL stands in for
    # it so `$` cannot match where the real text does not end.
    context = cand.pre + cand.group0 + cand.post + ("" if cand.eof else "\0")
    match = pattern.match(context, len(cand.pre))
    if match is None or match.group(0) != cand.group0:
        return None
    if (match.group(1) if pattern.groups else None) != cand.group1:
        return None
    return match


def _is_index(value: object, count: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < count


def _check_statuses(value: object, blobs: int) -> list[str]:
    """One status per blob: "t" text, "b" binary, or "u:<reason>" unmirrored."""
    if not isinstance(value, list) or len(value) != blobs:
        raise NativeAuditError(f"kernel status is not a list of {blobs} statuses")
    if not all(s in ("t", "b") or (isinstance(s, str) and s.startswith("u:")) for s in value):
        raise NativeAuditError("kernel reported a status that is not text, binary or unmirrored")
    return value


def _check_scan(answer: dict, statuses: list, rules: int) -> None:
    """A SCAN reply must answer the request: one status per blob, a LOAD status only ever moved to
    unmirrored, and candidates that name a text blob and a rule that was sent. Shape errors in the
    rest of the reply raise at the boundary in `audit_index_native`."""
    scanned = _check_statuses(answer["status"], len(statuses))
    for loaded, now in zip(statuses, scanned, strict=True):
        # LOAD fixed each blob's status; a SCAN may only unmirror a text blob, never flip one into another kind.
        if now != loaded and not (loaded == "t" and now.startswith("u:")):
            raise NativeAuditError(f"kernel scan changed a LOAD status {loaded!r} to {now!r}")
    for row in answer["cands"]:
        blob, pat, line, _group0, _group1, _pre, _post, eof = row
        # A wrong line or eof flag raises nothing: it would print a wrong report or change the re-check.
        if not (
            _is_index(blob, len(statuses))
            and _is_index(pat, rules)
            and answer["status"][blob] == "t"
            and type(line) is int
            and line >= 1
            and eof in (0, 1)
        ):
            raise NativeAuditError(f"kernel candidate does not fit the request: {str(row)[:200]!r}")


def _cands(answer: dict) -> dict[int, list[_Cand]]:
    out: dict[int, list[_Cand]] = {}
    for blob, pat, line, g0, g1, pre, post, eof in answer["cands"]:
        out.setdefault(blob, []).append(_Cand(pat, line, g0, g1, pre, post, bool(eof)))
    return out


class _Refused(Exception):
    """The kernel cannot mirror a pattern; `auto` hands the whole run to Python."""


def audit_index_native(root: Path, *, include_records: bool = False) -> audit.AuditResult | None:
    """The native audit, or None when the Python backend should run instead."""
    request = backend_request()
    if request == "python":
        return None
    binary = native_binary()
    if binary is None:
        if request == "native":
            raise NativeAuditError(
                f"{BACKEND_ENV}=native but the kernel is not built: cargo build --release "
                f"--manifest-path {CRATE_DIR.relative_to(CRATE_DIR.parents[2]).as_posix()}/Cargo.toml"
            )
        note("oss-tip-audit: native kernel not built, using the Python backend")
        return None
    kernel = _Kernel(binary)
    try:
        return _Run(kernel, root).run(include_records=include_records)
    except _Refused as exc:
        if request == "native":
            raise NativeAuditError(f"kernel refused the rules: {exc}") from exc
        note(f"oss-tip-audit: kernel cannot mirror the rules ({exc}); using the Python backend")
        return None
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        # Any reply without the shape the wrapper reads is a failed measurement, never a verdict.
        raise NativeAuditError(f"kernel reply does not fit the request: {exc!r}") from exc
    finally:
        kernel.close()


_Matches = dict[int, list[tuple[_Cand, re.Match[str]]]]


class _Run:
    """One native audit. Blob indexes are positions in `shas`, the LOAD order."""

    def __init__(self, kernel: _Kernel, root: Path) -> None:
        self.kernel = kernel
        self.root = root
        self.entries = audit._blob_entries(root)
        self.shas = list(dict.fromkeys(sha for _rel, mode, sha in self.entries if mode != _GITLINK))
        self.status: list[str] = []
        self.fallback: set[int] = set()
        self.raw: dict[str, bytes] = {}
        self.found: set[str] = set()
        self.path_rules: dict[str, list[audit.Finding]] = {}

    def run(self, *, include_records: bool) -> audit.AuditResult:
        if not self.entries:
            return audit.AuditResult((), 0, 0, (), 0, backend="native")
        self._load()
        specs = [(rule, pattern, RULE_PREFILTERS[rule]) for rule, pattern in PATTERNS]
        rule_matches = self._scan(specs, [1 if s == "t" else 0 for s in self.status], _PRE_WINDOW)
        known = self._harvest(rule_matches)
        login_matches: _Matches = {}
        if known.pattern is not None:
            modes = [1 if s == "t" and i not in self.fallback else 0 for i, s in enumerate(self.status)]
            spec = [(KNOWN_LOGIN_RULE, known.pattern, sorted(known.logins))]
            all_logins = self._scan(spec, modes, BARE_WINDOW)
            login_matches = {
                i: [(c, m) for c, m in pairs if is_bare_position(c.pre)] for i, pairs in all_logins.items()
            }
        return self._assemble(rule_matches, login_matches, known, include_records=include_records)

    def _load(self) -> None:
        git = shutil.which("git")
        if git is None:
            raise NativeAuditError("git is not on PATH, so the kernel cannot read the published blobs")
        self.kernel.send([b"LOAD\n", git, str(self.root), BINARY_SNIFF_BYTES, len(self.shas), *self.shas])
        # While the kernel reads the blobs: the pathnames, which Python audits itself
        # (git permits names that are not UTF-8). Known logins join in `_assemble`.
        for rel, _mode, _sha in self.entries:
            if rel not in self.path_rules:
                self.found |= logins_in_text(rel)
                self.path_rules[rel] = list(audit.findings_in_text(rel, rel, audit.NO_KNOWN_LOGINS))
        self.status = _check_statuses(self.kernel.receive()["status"], len(self.shas))

    def _scan(
        self, specs: Sequence[tuple[str, re.Pattern[str], Sequence[str]]], modes: list[int], pre: int
    ) -> _Matches:
        """Verified candidates per blob; blobs the kernel or the re-check gave up on fall back."""
        answer = _scan(self.kernel, specs, modes, pre)
        if "refused" in answer:
            raise _Refused(str(answer["refused"]))
        _check_scan(answer, self.status, len(specs))
        newly = {i for i, s in enumerate(answer["status"]) if s.startswith("u:")} - self.fallback
        matches: _Matches = {}
        for i, cands in _cands(answer).items():
            pairs = [(cand, _verified(specs[cand.pat][1], cand)) for cand in cands]
            if any(match is None for _cand, match in pairs):
                newly.add(i)
                continue
            matches[i] = pairs  # type: ignore[assignment]
        self.fallback |= newly
        self.raw.update(audit.read_blobs(self.root, [self.shas[i] for i in sorted(newly)]))
        return {i: pairs for i, pairs in matches.items() if i not in self.fallback}

    def _harvest(self, rule_matches: _Matches) -> audit.KnownLogins:
        """`_known_logins` over every published text, records included."""
        for i in self.fallback:
            text = audit._decode_text(self.raw[self.shas[i]])
            self.found |= logins_in_text(text) if text is not None else set()
        for pairs in rule_matches.values():
            for cand, match in pairs:
                rule = PATTERNS[cand.pat][0]
                login = revealed_login(rule, match, cand.pre) if rule in HOME_RULES else None
                self.found |= {login} if login else set()
        return audit.KnownLogins.from_logins(reviewed_logins(self.found))

    def _native_findings(
        self, rel: str, i: int, rule_matches: _Matches, login_matches: _Matches, known: audit.KnownLogins
    ) -> list[audit.Finding]:
        """`_audit_one`'s findings for a blob the kernel read, before the exemption split."""
        # `findings_in_text(rel, rel, known)`, in its order: known logins, then rules.
        bare = bare_login_matches(rel, rel.lower(), known.pattern, known.logins)
        own = [audit.Finding(rel, 0, KNOWN_LOGIN_RULE, m.group(0)) for m in bare]
        own += [audit.Finding(f.path, 0, f.rule, f.match) for f in self.path_rules[rel]]
        for cand, match in login_matches.get(i, ()):
            own.append(audit.Finding(rel, cand.line, KNOWN_LOGIN_RULE, match.group(0)))
        for cand, match in rule_matches.get(i, ()):
            rule = PATTERNS[cand.pat][0]
            if _rule_accepts(rule, match, cand.pre):
                own.append(audit.Finding(rel, cand.line, rule, match.group(0)))
        return own

    def _assemble(
        self, rule_matches: _Matches, login_matches: _Matches, known: audit.KnownLogins, *, include_records: bool
    ) -> audit.AuditResult:
        """Findings in exactly the order and shape `_audit_index_python` produces them."""
        # Every count is per published entry, so `python_fallback` and `scanned` share one population.
        counts = {"scanned": 0, "skipped_binary": 0, "records": 0, "python_fallback": 0}
        index = {sha: i for i, sha in enumerate(self.shas)}
        # Per entry, in entry order: (findings, exempt) already split by `_audit_one`,
        # or (blob id, findings) still waiting for the digest `is_exempt` needs.
        parts: list[tuple[list[audit.Finding], list[audit.Finding]] | tuple[str, list[audit.Finding]]] = []
        for rel, mode, sha in self.entries:
            done: tuple[list[audit.Finding], list[audit.Finding]] = ([], [])
            if not (include_records or not audit.is_record_path(rel)):
                counts["records"] += 1
                audit._audit_one(rel, b"", *done, scan_content=False, known=known)
            elif mode == _GITLINK:
                audit._audit_one(rel, b"", *done, known=known)
            elif index[sha] in self.fallback:
                read = audit._audit_one(rel, self.raw[sha], *done, known=known)
                counts["scanned" if read else "skipped_binary"] += 1
                counts["python_fallback"] += 1 if read else 0
            else:
                is_text = self.status[index[sha]] == "t"
                counts["scanned" if is_text else "skipped_binary"] += 1
                own = self._native_findings(rel, index[sha], rule_matches, login_matches, known)
                parts.append((sha, own))
                continue
            parts.append(done)
        # Read bytes only for blobs that have a finding to judge.
        need = [sha for sha, own in parts if isinstance(sha, str) and own and sha not in self.raw]
        self.raw.update(audit.read_blobs(self.root, list(dict.fromkeys(need))))
        findings: list[audit.Finding] = []
        exempt: list[audit.Finding] = []
        for first, second in parts:
            if isinstance(first, str):
                digest = sha256(self.raw[first]).hexdigest() if second else ""
                for f in second:
                    (exempt if audit.is_exempt(f, digest) else findings).append(f)
            else:
                findings += first
                exempt += second
        return audit.AuditResult(
            tuple(findings),
            counts["scanned"],
            counts["skipped_binary"],
            tuple(exempt),
            counts["records"],
            backend="native",
            python_fallback=counts["python_fallback"],
        )
