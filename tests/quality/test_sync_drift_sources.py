"""The SOURCE-TEXT scan behind NAIVE_DEFAULT_SOURCES, and its instrument.

Split out of test_sync_drift_lint.py (the 600-line file-size gate), which
keeps the checks that read a database's SHAPE. This file holds the one thing
D-05 cannot learn from a database: WHICH FILES declare a naive stamp default
at all. A ``DEFAULT CURRENT_TIMESTAMP`` in a module no scanned ladder builds
is invisible to D-05, and invisible reads exactly like clean, so the declared
set is compared against a fresh read of the tree.

TWO WAYS THIS SCAN CAN LIE, and both are guarded here.

It can look at the WRONG TREE. An ``rglob('*.py')`` walked
apps/desktop/src-tauri/payload/ and target/ -- gitignored, but present in any
tree that has run a Tauri build and holding the whole installed dependency
closure, where sqlalchemy alone declares a ``DEFAULT CURRENT_TIMESTAMP``. The
scan failed on a vendored third-party file and told the reader to give it a
migration ladder. The enumeration is now ``git ls-files``, which is the tree
rather than whatever a build left in it, minus the quality gate's OWN
:data:`~scripts.quality_gate.CFG.VENDORED` and
:data:`~scripts.quality_gate.CFG.DERIVED` prefixes, imported rather than
re-listed so the two cannot drift apart. And it is not restricted to
``*.py``: that lens is the same blind spot D-08 hit for real, and it cannot
see a naive default declared in .rs, .sql or .ts.

It can match the WRONG TEXT. ``DEFAULT CURRENT_TIMESTAMP`` appears in PROSE
all over this repo -- most pointedly in the remediation D-05 itself prints,
which spells out the very phrase it is telling you to remove. A file that
merely NAMES the phrase declares no default and needs no ladder, so matching
raw text sends a developer off to give a docstring a migration ladder, and
the fastest way to get a gate switched off is a report that is wrong. The
match is therefore DDL-SCOPED: the phrase must sit inside a CREATE TABLE
column list, bounded by that statement's own parentheses.

Regression lines:
  - if a file outside NAIVE_DEFAULT_SOURCES declares a naive default in DDL
    and this scan stays silent then broken
  - if the scan reports a file that only names the phrase in prose then broken
  - if the scan reports nothing at all then it is unmeasured, not clean, so
    broken
  - if the tracked-file probe reports nothing for a token the tree contains
    then broken
  - if the tracked-file probe raises or matches everything for a token the
    tree does not contain then broken
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

from scripts import sync_drift_rules as rules
from scripts.quality_gate import CFG as GATE

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: The naive stamp default, as DDL writes it. Deliberately blunt: its job is
#: to name FILES that need a ladder before D-05 can see them, not to decide
#: whether any one default is a defect. That decision is D-05's, and it makes
#: it by EVALUATING the expression against sqlite rather than reading it.
_DEFAULT_NOW = re.compile(r"\bDEFAULT\s+CURRENT_TIMESTAMP\b", re.IGNORECASE)

#: A CREATE TABLE header, up to and including the ``(`` that opens its column
#: list. The table name is part of the pattern, and that is what stops prose
#: from opening a block: between the keyword and the paren there may be a
#: name and nothing else, so "names a ``CREATE TABLE``, with one line saying"
#: and "runs 25 CREATE TABLE statements, 22 of them" both fail to match.
_DDL_HEAD = re.compile(
    r"\bCREATE\s+(?:(?:VIRTUAL|TEMP|TEMPORARY)\s+)*TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"[\"'`\[]?\w+[\"'`\]]?(?:\.[\"'`\[]?\w+[\"'`\]]?)?"
    r"(?:\s+USING\s+\w+)?"
    r"\s*\(",
    re.IGNORECASE,
)


def _ddl_column_lists(text: str) -> Iterator[str]:
    """Every CREATE TABLE column list in ``text``, bounded by its own parens.

    Structural rather than line-based, so a default split across lines is
    still inside its block and no formatting choice can hide one. Keyed on
    SQL rather than on a host language, so it reads .py, .rs, .sql and .ts
    alike -- the same reason the authority scan stopped being ``*.py``.

    Unbalanced parentheses RAISE. A column list with no end means the file
    was not really scanned, and returning the matches found so far would be a
    partial measurement wearing a complete one's clothes.
    """
    for head in _DDL_HEAD.finditer(text):
        start, depth = head.end() - 1, 0
        for index in range(start, len(text)):
            depth += (text[index] == "(") - (text[index] == ")")
            if depth == 0:
                yield text[start : index + 1]
                break
        else:
            raise AssertionError(
                f"unbalanced parentheses after {text[head.start() : head.end()]!r}: "
                "the column list has no end, so this file was not really scanned"
            )


def declares_a_naive_default(text: str) -> bool:
    """True when ``text`` declares a naive stamp default IN DDL, not in prose."""
    return any(_DEFAULT_NOW.search(block) for block in _ddl_column_lists(text))


def _tracked_files_where(predicate: Callable[[str], bool], *roots: str) -> set[str]:
    """Tracked files under ``roots`` whose text satisfies ``predicate``.

    ``git ls-files`` rather than a filesystem walk: it is the tree, not
    whatever a build left lying in it. The gate's own vendored and derived
    prefixes come off on top, because a third-party file checked into the
    tree is still not this repo's schema. Undecodable bytes are replaced
    rather than raising, so a binary blob under a scanned root is a non-match
    instead of an error wearing the costume of a finding.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z", *roots],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    excluded = (*GATE.VENDORED, *GATE.DERIVED)
    names = [
        name
        for name in listing.split("\0")
        if name and not name.startswith(excluded)
    ]
    assert names, f"git ls-files returned nothing for {roots}; the scan root is wrong"
    return {
        name
        for name in names
        if predicate((REPO_ROOT / name).read_text(encoding="utf-8", errors="replace"))
    }


def _tracked_files_matching(pattern: re.Pattern[str], *roots: str) -> set[str]:
    """Tracked files under ``roots`` whose RAW TEXT matches ``pattern``."""
    return _tracked_files_where(lambda text: bool(pattern.search(text)), *roots)


# ----- the declaration, against a fresh read of the tree ----------------------


def test_d05_scans_every_file_that_declares_a_naive_default() -> None:
    """D-05's floors prove it measured A database, not the RIGHT databases.

    Both sides are derived -- this declaration and what the source says today
    -- so neither can rot, and a new file that starts minting naive stamps
    fails HERE, by name, instead of silently sitting outside the subject.
    """
    found = _tracked_files_where(declares_a_naive_default, "apps")

    assert found, (
        "the source scan found no DEFAULT CURRENT_TIMESTAMP in any CREATE "
        "TABLE under apps/. An empty result is what a broken walk returns "
        "too, so this is an unmeasured tree, not a clean one."
    )
    assert found == set(rules.NAIVE_DEFAULT_SOURCES), (
        "files declaring a naive stamp default have changed. Newly found: "
        f"{sorted(found - set(rules.NAIVE_DEFAULT_SOURCES))}; no longer "
        f"found: {sorted(set(rules.NAIVE_DEFAULT_SOURCES) - found)}. A new "
        "file here needs a ladder in scripts.sync_drift_subject.OTHER_LADDERS "
        "before D-05 can see it at all."
    )


# ----- the instrument, validated in both directions ---------------------------


def test_the_source_scan_reads_ddl_and_not_prose() -> None:
    """The control the raw-text version could not pass, on REAL repo text.

    Derived from the hypothesis rather than from a clean corpus: the claim is
    that this scan reports DECLARERS and not MENTIONERS, so it is pointed at
    the place the claim predicts a mention with no declaration -- scripts/,
    where the linter's own remediation spells the phrase out at the reader.
    A control drawn from files that never mention it could pass no matter
    which way the classifier was wired.

    Both halves are asserted, because either alone is satisfied by a broken
    classifier: one that answered False always would pass the prose half, and
    one that answered True always would pass the DDL half.
    """
    mentions = _tracked_files_matching(_DEFAULT_NOW, "scripts")
    declares = _tracked_files_where(declares_a_naive_default, "scripts")

    assert "scripts/sync_drift_rules.py" in mentions, (
        "the control needs a file that really does name the phrase in prose. "
        "If D-05's remediation stopped naming it, point this at another one "
        "rather than deleting the control."
    )
    assert declares == set(), (
        f"{sorted(declares)} name DEFAULT CURRENT_TIMESTAMP in prose only, "
        "and the DDL scan reported them as declaring one. A gate that sends "
        "a developer to give a docstring a migration ladder gets switched off."
    )


def test_the_ddl_scope_fires_on_a_column_definition_and_not_on_a_comment() -> None:
    """The same discrimination on text this test owns, shape by shape.

    The real-tree control proves the classifier says NO to today's prose. It
    cannot prove the classifier says YES to a DDL shape this repo does not
    happen to use yet, and a scan that silently stopped recognising one would
    read exactly like a tree that stopped declaring them.
    """
    multi_line = """
        CREATE TABLE IF NOT EXISTS gig_notes (
            gig_id      TEXT PRIMARY KEY,
            created_at  TIMESTAMP NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
        )
    """
    virtual = "CREATE VIRTUAL TABLE lyrics_fts USING fts5(line DEFAULT CURRENT_TIMESTAMP)"
    nested = "CREATE TABLE t (a NUMERIC(10, 2), b TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    comment = "# DEFAULT CURRENT_TIMESTAMP makes SQLite write a NAIVE stamp"
    prose_near_ddl = (
        'The ladder runs 25 CREATE TABLE statements, and not one of them may '
        'say DEFAULT CURRENT_TIMESTAMP. See table_docs.py for the wording.'
    )
    after_the_block = "CREATE TABLE t (a TEXT)\n# never DEFAULT CURRENT_TIMESTAMP here"

    assert declares_a_naive_default(multi_line)
    assert declares_a_naive_default(virtual)
    assert declares_a_naive_default(nested)
    assert not declares_a_naive_default(comment)
    assert not declares_a_naive_default(prose_near_ddl)
    assert not declares_a_naive_default(after_the_block)


def test_the_tracked_file_probe_can_find_something_and_can_report_absent() -> None:
    """The enumeration behind both scans, validated both ways.

    A scan that finds nothing proves nothing until it has been shown able to
    find something, and an empty result is also what a broken walk, a bad
    pattern and a wrong root all return. So: a pattern the tree certainly
    contains must come back non-empty, and a pattern nothing contains must
    come back empty rather than raising or matching everything.
    """
    present = _tracked_files_matching(re.compile(r"\bSCHEMA_VERSION\b"), "apps")
    absent = _tracked_files_matching(re.compile(r"af_probe_no_such_token_anywhere"), "apps")

    assert "apps/shared/state/schema.py" in present
    assert absent == set()


def test_the_scan_drops_the_gate_s_own_vendored_and_derived_prefixes() -> None:
    """The exclusion is IMPORTED from the gate, so it cannot drift from it.

    Re-listing those prefixes here would create a second declaration of what
    counts as this repo's own code, free to disagree with the one the gate
    scores against. The control is that the exclusion really is non-empty:
    an emptied tuple would silently widen every scan in this file.
    """
    assert GATE.VENDORED and GATE.DERIVED
    everything = _tracked_files_where(lambda _text: True, "apps")

    assert not [name for name in everything if name.startswith(GATE.VENDORED)]
    assert not [name for name in everything if name.startswith(GATE.DERIVED)]
