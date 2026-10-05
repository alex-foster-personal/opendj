"""Audit the tracked tree for identity and infrastructure strings that must not ship public.

Companion to docs/oss-going-public-checklist.md. The checklist's REVIEW table was a
one-off grep whose literal search strings were themselves scrubbed by #910 and #1326, so
the numbers there cannot be re-derived. This module pins the INVARIANT instead: the
tracked tree carries no real home directory, no consumer mailbox, no tailnet name and no
CGNAT address, and no login the tree reveals in a home path written as a bare word
anywhere else (scripts/oss_tip_logins.py). It is run by tests/scripts/test_oss_tip_audit.py on every CI pass, so a
regression fails a PR rather than surfacing in a public fork.

    python -m scripts.oss_tip_audit            # scan the tracked tree, exit 1 on findings
    python -m scripts.oss_tip_audit --root X   # another checkout

Write-once session records (RECORD_PATHS in scripts/oss_tip_rules.py: `.planning/`,
`specs/`, `app_docs/`, `docs/threads/`) have their CONTENT skipped and their paths
COUNTED, because rewriting a dated record of what was actually run destroys the
evidence without removing anything still reachable. Pathnames everywhere are still
audited. `--include-records` scans the records too; the count is printed either way
so a scoped-out run cannot be mistaken for a clean one.

Placeholder home directories the docs and tests use on purpose stay allowed
(PLACEHOLDER_USERS). Binary blobs are skipped and counted, so a zero-findings run also
reports how much it actually read; size alone never exempts a file from the scan.

It reads HEAD AND the index, never the working tree, because those two diverge in
both directions and each divergence hid a real leak. One consequence surprises
people, including its author: an uncommitted fix does NOT turn the gate green,
because HEAD still holds the blob it is fixing. That is the correct answer rather
than a rough edge -- publishing right now would publish HEAD -- but it means the
gate goes green on the COMMIT, not on the edit.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import os
import re
import subprocess
import sys
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from scripts.oss_tip_logins import RULE as KNOWN_LOGIN_RULE
from scripts.oss_tip_logins import (
    bare_login_matches,
    known_login_pattern,
    revealed_logins,
)
from scripts.oss_tip_rules import (
    ALLOWED_MAILBOXES,
    ALLOWED_NON_ADDRESSES,
    BINARY_SNIFF_BYTES,
    GENERATED_TEST_ID_PATHS,
    MAILBOX_EXEMPT_PATHS,
    PATTERNS,
    PLACEHOLDER_USERS,
    RECORD_PATHS,
    RULE_PREFILTERS,
    _rule_accepts,
    is_record_path,
)

__all__ = [
    "ALLOWED_MAILBOXES",
    "ALLOWED_NON_ADDRESSES",
    "BINARY_SNIFF_BYTES",
    "GENERATED_TEST_ID_PATHS",
    "MAILBOX_EXEMPT_PATHS",
    "UPSTREAM_LICENSE_MAILBOX_BLOBS",
    "PATTERNS",
    "PLACEHOLDER_USERS",
    "RECORD_PATHS",
    "RULE_PREFILTERS",
    "AuditResult",
    "Finding",
    "KnownLogins",
    "audit_index",
    "audit_paths",
    "audit_tracked_tree",
    "findings_in_text",
    "is_exempt",
    "is_record_path",
    "published_blobs",
]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    match: str

    def render(self) -> str:
        # A surrogate-escaped pathname cannot be encoded to UTF-8, so printing
        # one raw would trade the decode crash for an encode crash one line
        # later. `backslashreplace` keeps the bytes legible and the gate alive.
        path = self.path.encode("utf-8", "backslashreplace").decode("utf-8")
        return f"{path}:{self.line}: {self.rule}: {self.match}"


@dataclass(frozen=True)
class AuditResult:
    findings: tuple[Finding, ...]
    scanned: int
    skipped_binary: int
    exempt: tuple[Finding, ...] = ()
    # Paths whose CONTENT was skipped because it is a write-once record
    # (RECORD_PATHS). Counted, never silent: a run that reads nothing must not be
    # mistakable for a run that found nothing.
    records: int = 0

    def summary(self) -> str:
        return (
            f"scanned={self.scanned} skipped_binary={self.skipped_binary} "
            f"records={self.records} exempt={len(self.exempt)} "
            f"findings={len(self.findings)}"
        )


@dataclass(frozen=True)
class KnownLogins:
    """Logins the published tree reveals in a home-directory position (oss_tip_logins).

    Built once per audit from EVERY published text, records included, and then applied
    to the scanned texts only, so a record can teach the gate a login without its own
    content being judged.
    """

    logins: frozenset[str] = frozenset()
    pattern: re.Pattern[str] | None = None

    @classmethod
    def from_texts(cls, texts: Iterable[str]) -> KnownLogins:
        logins = revealed_logins(texts)
        return cls(logins, known_login_pattern(logins))


NO_KNOWN_LOGINS = KnownLogins()

# Verbatim upstream license mirrors whose author mailboxes are part of the
# attribution text. Path AND sha256 of the published blob audit_index already
# holds: a future file under docs/legal/ is not exempt, and an edited copy at
# a listed path is not exempt. Only consumer-mailbox is waived. Pins match
# scripts/license_mirrors.py REVIEWED_LICENSE_TEXTS for these paths.
UPSTREAM_LICENSE_MAILBOX_BLOBS: Mapping[str, str] = MappingProxyType(
    {
        "docs/legal/lukeed-MIT.txt": (
            "ba573393f24555ac0528612ad39665fab5bdcc80330a61096024bbf5f736526d"
        ),
        "docs/legal/objc2-licenses.txt": (
            "2001f1ac74823ea95c52652785873026e36246088d72908175eeb4a3075e015e"
        ),
        "docs/legal/realfft-3.5.0-LICENSE.txt": (
            "8eb17835ae38101a31dca0aa580fefded3fa604c9b6a3f761faa8b84fb63b061"
        ),
        "docs/legal/rollup-LICENSE.md": (
            "fa1bd040c5bdeefe65b3821cebf474f2733ce65df13089bd151dda1778e62fe8"
        ),
    }
)


# ----- matching ------------------------------------------------------------------------


def is_exempt(finding: Finding, blob_sha256: str = "") -> bool:
    """True for a mailbox on a reviewed identity surface or pinned license blob.

    Reported and counted separately rather than dropped: an exemption that hides
    its own matches cannot be checked, and a NEW address in one of these files
    must still be visible to whoever runs the gate. The verbatim upstream license
    mirrors under docs/legal/python-build-standalone/ are enumerated exactly in
    MAILBOX_EXEMPT_PATHS (Codex P1, PR #4853 r4170573408), not prefix-matched, so
    a future file added to that directory is NOT auto-exempt. Identity-surface
    and PBS paths stay path-only. The UPSTREAM_LICENSE_MAILBOX_BLOBS paths also
    require the published blob SHA256 to equal the reviewed pin.
    """
    if finding.rule != "consumer-mailbox":
        return False
    if finding.path in MAILBOX_EXEMPT_PATHS or finding.path in GENERATED_TEST_ID_PATHS:
        return True
    expected = UPSTREAM_LICENSE_MAILBOX_BLOBS.get(finding.path)
    return expected is not None and blob_sha256 == expected


def findings_in_text(
    path: str, text: str, known: KnownLogins = NO_KNOWN_LOGINS
) -> Iterator[Finding]:
    """One pass per rule over the WHOLE text, with line numbers derived from the
    match offset.

    The obvious loop -- for each line, for each rule -- re-enters the regex engine
    once per line per rule, and the constant is what dominates: on this tree that
    is ~26,000 lines x 6 patterns of setup cost, which took the full-tree scan from
    seconds to minutes when two rules gained IGNORECASE. Scanning whole text is
    equivalent here because no pattern can span a newline: every one either
    excludes whitespace explicitly or is a single-token shape. That equivalence is
    a property of the patterns, so the test asserts it rather than trusting it.
    """
    lowered = text.lower()
    line_starts: list[int] | None = None
    for match in bare_login_matches(text, lowered, known.pattern, known.logins):
        line_starts = line_starts or _line_starts(text)
        yield Finding(path, bisect_right(line_starts, match.start()), KNOWN_LOGIN_RULE, match.group(0))
    live = [
        (rule, pattern)
        for rule, pattern in PATTERNS
        if any(token in lowered for token in RULE_PREFILTERS[rule])
    ]
    if not live:
        return
    line_starts = line_starts or _line_starts(text)
    for rule, pattern in live:
        for match in pattern.finditer(text):
            # A bounded window of what precedes the match, for the two rules that
            # need context rather than the token alone (a URL authority before a
            # home path). 256 characters is far more than any authority needs and
            # keeps the per-match cost independent of file size.
            preceding = text[max(0, match.start() - 256) : match.start()]
            if _rule_accepts(rule, match, preceding):
                yield Finding(path, bisect_right(line_starts, match.start()), rule, match.group(0))


def _line_starts(text: str) -> list[int]:
    starts = [0]
    start = text.find("\n")
    while start != -1:
        starts.append(start + 1)
        start = text.find("\n", start + 1)
    return starts


# ----- tree walk -----------------------------------------------------------------------


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    ).stdout
    return [root / os.fsdecode(p) for p in out.split(b"\0") if p]


def _blob_entries(root: Path) -> list[tuple[str, str, str]]:
    """(rel, mode, sha) for every path git would publish, from HEAD AND the index.

    Both, because the two diverge in both directions and each divergence hid a
    real leak (Codex P1, #1440, twice):

    - content scrubbed only in the WORKING TREE left the leaking blob in the
      index, and reading the filesystem certified it clean;
    - content scrubbed only in the INDEX left the leaking commit in HEAD, which
      is what a visibility flip publishes right now.

    Auditing the union costs almost nothing -- entries are deduplicated by blob
    id, and a clean tree has one blob per path -- and it removes the question of
    which single source is the right one. HEAD alone is not: the index is what
    HEAD becomes on the next commit, and this gate runs before that commit.

    An unborn HEAD (a repository with no commit yet) contributes nothing, which
    is correct: there is no committed tree to publish. Every OTHER `ls-tree`
    failure is fatal, and that distinction is the whole point of
    `_head_is_unborn` below.
    """
    entries: dict[tuple[str, str], tuple[str, str, str]] = {}

    def absorb(raw: bytes, *, sha_field: int) -> None:
        # Both commands emit `<meta>\t<path>\0`, but the meta differs:
        # `ls-files -s` is `mode sha stage`, `ls-tree -r` is `mode type sha`.
        # The SHA is therefore at a different index, and reading the wrong one
        # silently produces entries that cat-file cannot resolve.
        for record in raw.split(b"\0"):
            if not record:
                continue
            meta, _, raw_path = record.partition(b"\t")
            fields = meta.split()
            # Git permits a pathname that is not valid UTF-8, and a plain
            # `.decode()` raised UnicodeDecodeError before a single blob was
            # read -- so the MANDATORY gate crashed and blocked CI instead of
            # reporting a result (Codex P2, #1440, discussion_r3972967010).
            # `os.fsdecode` is the surrogate-escape convention, which round
            # trips those bytes instead of losing them. Mode and sha are hex
            # ASCII by construction, so they decode plainly.
            rel = os.fsdecode(raw_path)
            mode = fields[0].decode()
            sha = fields[sha_field].decode()
            entries.setdefault((rel, sha), (rel, mode, sha))

    absorb(
        subprocess.run(
            ["git", "-C", str(root), "ls-files", "-s", "-z"],
            capture_output=True,
            check=True,
        ).stdout,
        sha_field=1,
    )
    head = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "-z", "HEAD"],
        capture_output=True,
        check=False,
    )
    if head.returncode == 0:
        absorb(head.stdout, sha_field=2)
    elif not _head_is_unborn(root):
        # A tool that cannot measure must report UNKNOWN, never a verdict.
        # Dropping HEAD on ANY non-zero exit meant a partial clone that cannot
        # fetch the committed tree, or a missing HEAD object, audited the index
        # alone; with a staged scrub in the index that returns `findings=0` for
        # a commit nobody read, and this gate's whole job is to say whether the
        # thing about to be published is clean (Codex P1 BLOCKING, #1440,
        # discussion_r3975241642).
        raise OSError(
            "oss-tip-audit cannot read the committed tree: "
            f"`git ls-tree -r HEAD` exited {head.returncode}. HEAD exists, so "
            "this is not an unborn repository; auditing the index alone would "
            "report a clean result for a commit that was never inspected. "
            f"stderr: {head.stderr.decode(errors='replace').strip()}"
        )
    return list(entries.values())


def _head_is_unborn(root: Path) -> bool:
    """True ONLY for a repository whose HEAD names a branch with no commit yet.

    Deliberately not "HEAD did not resolve". A partial clone missing the tree,
    a corrupt object, or a `ls-tree` killed by a signal all fail to resolve
    too, and folding them in with the unborn case is what turned an unreadable
    commit into a clean report.

    A DETACHED HEAD names a commit directly, so it is born by construction and
    this returns False without consulting anything further.
    """
    branch = subprocess.run(
        ["git", "-C", str(root), "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        check=False,
    )
    if branch.returncode != 0:
        return False
    ref = branch.stdout.decode().strip()
    return (
        subprocess.run(
            ["git", "-C", str(root), "show-ref", "--verify", "--quiet", ref],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


def published_blobs(root: Path) -> Iterator[tuple[str, str, bytes]]:
    """Every publishable path with the bytes git holds for it.

    A symlink's blob IS its target text, so that case needs no special handling
    here; git stores exactly what it publishes. A gitlink has no blob, so it
    yields empty content -- its PATHNAME is still audited by the caller, because
    git publishes the name of a submodule even though the object is not a blob
    (Codex P1, #1440).
    """
    entries = _blob_entries(root)
    if not entries:
        return
    blobs = [e for e in entries if e[1] != "160000"]
    contents: dict[str, bytes] = {}
    if blobs:
        proc = subprocess.run(
            ["git", "-C", str(root), "cat-file", "--batch"],
            input="\n".join(sha for _, _, sha in blobs).encode(),
            capture_output=True,
            check=True,
        ).stdout
        offset = 0
        for _rel, _mode, sha in blobs:
            header_end = proc.index(b"\n", offset)
            size = int(proc[offset:header_end].split()[2])
            body_start = header_end + 1
            contents[sha] = proc[body_start : body_start + size]
            offset = body_start + size + 1  # git writes a newline after each object
    for rel, mode, sha in entries:
        yield rel, mode, contents.get(sha, b"")


# Byte-order marks, longest first: UTF-32's BOMs START with UTF-16's, so
# matching UTF-16 first would decode a UTF-32 file with the wrong codec.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _decode_text(raw: bytes) -> str | None:
    """The file's text, or None when it is genuinely binary.

    The NUL sniff alone is wrong for UTF-16 and UTF-32, which are TEXT whose
    encoded bytes are full of NULs: a Windows-generated export holding a real
    home path or mailbox was skipped whole and the gate certified it clean at
    findings=0, which is the one failure mode a publication gate may not have
    (Codex P1, #1440). A BOM is decisive where the heuristic is not, so it is
    consulted FIRST and the heuristic only rules on what has no BOM.
    """
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace")
    if b"\0" in raw[:BINARY_SNIFF_BYTES]:
        return None
    return raw.decode("utf-8", errors="replace")


def _audit_one(
    rel: str,
    raw: bytes,
    findings: list[Finding],
    exempt: list[Finding],
    *,
    scan_content: bool = True,
    known: KnownLogins = NO_KNOWN_LOGINS,
) -> bool:
    """Audit one path plus its bytes. True when the CONTENT was read."""
    # Hash the bytes this walk already holds (the published blob in audit_index,
    # the filesystem bytes in audit_paths). Re-reading the working tree here
    # would certify a scrub that git is not about to publish.
    digest = hashlib.sha256(raw).hexdigest()
    # Line 0: the name, not a line of the content. Done for EVERY tracked path,
    # including binaries and including records, because `git ls-files` prints the
    # name of a blob nobody opens (Codex P1, #1440) and a record FILENAME must not
    # be a way to smuggle an identity past the gate (#1808).
    for finding in findings_in_text(rel, rel, known):
        (exempt if is_exempt(finding, digest) else findings).append(
            Finding(finding.path, 0, finding.rule, finding.match)
        )
    if not scan_content:
        return False
    text = _decode_text(raw)
    if text is None:
        return False
    for finding in findings_in_text(rel, text, known):
        (exempt if is_exempt(finding, digest) else findings).append(finding)
    return True


def _known_logins(entries: Iterable[tuple[str, bytes]]) -> KnownLogins:
    """Harvest from EVERY publishable path and its text, records included.

    Records are read here although their content is never judged: a record that
    quotes a real home directory is exactly how the gate learns that login is real.
    """

    def texts() -> Iterator[str]:
        for rel, raw in entries:
            yield rel
            text = _decode_text(raw)
            if text is not None:
                yield text

    return KnownLogins.from_texts(texts())


def _filesystem_bytes(path: Path) -> bytes:
    if path.is_symlink():
        return os.readlink(path).encode()
    return path.read_bytes() if path.is_file() else b""


def _is_content_scanned(rel: str, *, include_records: bool) -> bool:
    return include_records or not is_record_path(rel)


def audit_index(root: Path, *, include_records: bool = False) -> AuditResult:
    """The publication gate: what git would ship, read from git."""
    findings: list[Finding] = []
    exempt: list[Finding] = []
    scanned = skipped_binary = records = 0
    blobs = list(published_blobs(root))
    known = _known_logins((rel, raw) for rel, _mode, raw in blobs)
    for rel, mode, raw in blobs:
        if not _is_content_scanned(rel, include_records=include_records):
            _audit_one(rel, b"", findings, exempt, scan_content=False, known=known)
            records += 1
            continue
        if mode == "160000":
            # A submodule has no blob to read, but git publishes its PATHNAME.
            _audit_one(rel, b"", findings, exempt, known=known)
            continue
        if _audit_one(rel, raw, findings, exempt, known=known):
            scanned += 1
        else:
            skipped_binary += 1
    return AuditResult(tuple(findings), scanned, skipped_binary, tuple(exempt), records)


def audit_paths(root: Path, paths: Iterable[Path], *, include_records: bool = False) -> AuditResult:
    """Filesystem variant, for auditing a directory that is not a git index."""
    findings: list[Finding] = []
    exempt: list[Finding] = []
    scanned = skipped_binary = records = 0
    paths = list(paths)
    known = _known_logins(
        (path.relative_to(root).as_posix(), _filesystem_bytes(path)) for path in paths
    )
    for path in paths:
        rel = path.relative_to(root).as_posix()
        if not _is_content_scanned(rel, include_records=include_records):
            _audit_one(rel, b"", findings, exempt, scan_content=False, known=known)
            records += 1
            continue
        if path.is_symlink():
            # Git publishes the LINK TEXT. `is_file()` follows the link instead:
            # a broken one vanished from the scan entirely, and a working one had
            # the target's contents audited in place of the target path git
            # actually ships (Codex P1, #1440).
            _audit_one(rel, os.readlink(path).encode(), findings, exempt, known=known)
            scanned += 1
            continue
        if not path.is_file():
            continue
        if _audit_one(rel, path.read_bytes(), findings, exempt, known=known):
            scanned += 1
        else:
            skipped_binary += 1
    return AuditResult(tuple(findings), scanned, skipped_binary, tuple(exempt), records)


def audit_tracked_tree(root: Path, *, include_records: bool = False) -> AuditResult:
    return audit_index(root, include_records=include_records)


# ----- cli -----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--include-records",
        action="store_true",
        help="also scan the content of write-once record directories",
    )
    args = parser.parse_args(argv)
    result = audit_tracked_tree(args.root.resolve(), include_records=args.include_records)
    for finding in result.exempt:
        print(f"exempt {finding.render()}")
    for finding in result.findings:
        print(finding.render())
    print(f"[{'FAIL' if result.findings else 'OK'}] oss-tip-audit {result.summary()}")
    return 1 if result.findings else 0


if __name__ == "__main__":
    sys.exit(main())
