"""Is every rekordbox write path in ``apps/`` actually IN the map?

The one-way gate is only as good as its inventory, so this module sweeps the
tree from two directions and refuses to let an unclassified writer exist:

  * BY NAME -- a module that mentions a live rekordbox target must be a mapped
    guard site or allowlisted as import-direction-only;
  * BY PROVENANCE -- a module that CONSTRUCTS a rekordbox DB handle must be a
    mapped guard site or allowlisted as read-only, with a written reason.

The second sweep is the structural one, and it exists because the first is not
enough. A writer that takes its target from an argument (``--rb-db``) or a CLI
flag never mentions a live path, and a writer handed an already-open handle
(``write_cues(db, ...)``) has no path at all. Both shapes were real, armed
live-write lanes in this tree, and both sat behind a fully green suite.

Regression lines:
  - if a new apps/ module names REKORDBOX_LIVE_DB while unmapped then broken
  - if a new apps/ module opens a rekordbox DB handle while unmapped then broken
  - if an allowlist entry no longer has the sites its reason describes then the
    reason was never re-checked, so broken
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from apps.shared.rekordbox_writeback import WRITE_SURFACES

# 02: classification by who OPENS the target, which is what the constructor
# and injected-handle sweeps here enforce.
pytestmark = pytest.mark.requirement("SYNC-ONEWAY-02")

REPO_ROOT = Path(__file__).resolve().parents[1]

# Build OUTPUT that lands under apps/: `just dmg` stages the engine payload (a
# copy of this repo's Python plus its installed dependency closure, pyrekordbox
# included) into apps/desktop/src-tauri/payload, and tauri-build mirrors it
# into target/. Derived bytes, gitignored, rebuilt from scratch -- sweeping
# them re-flags every already-classified surface a second time through its
# copy. Mirrors CFG.DERIVED in scripts/quality_gate.py.
DERIVED_BUILD_OUTPUT = (
    "apps/desktop/src-tauri/payload/",
    "apps/desktop/src-tauri/target/",
)

# The gate suite itself. Every test in these files must build its paths from
# tmp_path; see test_no_gate_test_can_name_a_real_library_location.
GATE_SUITE = (
    "tests/test_rekordbox_writeback_gate.py",
    "tests/test_rekordbox_writeback_recovery.py",
    "tests/test_rekordbox_writeback_surfaces.py",
)

# Ways a test could reach the user's actual library instead of a fixture: the
# home escapes are how an absolute real path gets into a test at all, and the
# live constants ARE the real library, so they may only ever be redirected
# (monkeypatch.setattr), never read as a value.
#
# These lines carry VOCABULARY_MARKER because the sweep below reads this very
# file: without it, naming a banned token here would count as using it.
VOCABULARY_MARKER = "names a ban, does not use it"
HOME_ESCAPES = (
    "Path.home(",  # names a ban, does not use it
    "expanduser",  # names a ban, does not use it
    'environ["HOME"]',  # names a ban, does not use it
    "environ['HOME']",  # names a ban, does not use it
)
LIVE_CONSTANTS = (  # names a ban, does not use it
    "paths.REKORDBOX_LIVE_DB",  # names a ban, does not use it
    "paths.REKORDBOX_WORKING_DB",  # names a ban, does not use it
    "platform_paths.SHARE_ROOT",  # names a ban, does not use it
)

# Modules that NAME a live rekordbox target but only ever read from it, or
# copy it INTO the app. Import direction is the whole point of this gate, so
# these are allowed -- each with the reason it is allowed, so a future reader
# can audit the list instead of trusting it.
IMPORT_DIRECTION_ONLY: dict[str, str] = {
    "apps/shared/platform_paths.py": "defines the constants; touches nothing",
    "apps/shared/paths.py": "copy_live_dbs snapshots live -> data/, import direction",
    "apps/shared/rekordbox_db.py": "opens the WORKING copy; live path only in an error string",
    "apps/shared/library_integrity.py": "reads to report integrity",
    "apps/shared/state/ingest/rekordbox.py": "ingests rekordbox -> state.db, import direction",
    "apps/shared/rekordbox_writeback.py": "the gate itself",
    "apps/shared/fd_anchored_walk.py": (
        "resolves asset paths under SHARE_ROOT via read-only directory-fd "
        "opens (O_RDONLY|O_DIRECTORY|O_NOFOLLOW) plus fstat/readlink to "
        "recover a path; never writes, creates, renames or unlinks anything. "
        "If a future change makes this module write, that write needs its "
        "own WriteSurface + guard -- this entry stops being true the moment "
        "an os.open here gains O_WRONLY/O_CREAT/O_TRUNC, or os.rename/"
        "os.unlink/os.mkdir appears"
    ),
    "apps/audit/session_history.py": (
        "reads play history. SHARP EDGE, kept on purpose so the next reader "
        "sees it: open_db(REKORDBOX_LIVE_DB) hands back a READ-WRITE handle on "
        "the live DB and only SELECTs are issued today. Any write added here "
        "needs a WriteSurface, not an edit to this reason"
    ),
    "apps/sync/playlist_apply.py": "writes djay; reads the rb live db for TSAF leaf validation",
    "apps/webui/crate_sync.py": (
        "reads SHARE_ROOT, writes the replica crate only -- and _assert_within_crate "
        "now ENFORCES that on the local-copy lane, not just the ssh-pull lane"
    ),
    "apps/sync/djay_sync_service.py": (
        "names REKORDBOX_LIVE_DB only in plan/diff JSON and audit reports; "
        "open_db() reads the working copy for diffs; live writes go through "
        "mapped http.rb_djay_sync.* WriteSurfaces guarded in routes/rb_djay_sync.py"
    ),
    "apps/sync/usb/pioneer/reader.py": "reads an exportLibrary.db",
    "apps/sync/usb/pioneer/differ.py": "diffs two exportLibrary.db reads",
    "apps/sync/usb/pioneer/__init__.py": "package docstring",
    "apps/sync/usb/pioneer/writer_onelibrary.py": "reached only via the two mapped USB entrypoints",
    "apps/sync/usb/pioneer/onelibrary.py": (
        "SQLCipher handle only; it opens whatever path its caller passes: the "
        "writer's template copy, or readers' tempfile copies"
    ),
    "apps/sync/usb/pioneer/value_verify.py": (
        "read-only USB stick value verify (key / PQTZ / loudness counts); "
        "never writes exportLibrary.db, ANLZ, or a desktop master.db"
    ),
    "apps/sync/usb/pioneer/value_verify_sidecar.py": (
        "loudness probe of a tempfile copy of exportLibrary.db; never opens "
        "the stick in-place. If a future change writes the copy back over the "
        "source, this entry stops being true"
    ),
}

LIVE_TARGET_MARKERS = ("REKORDBOX_LIVE_DB", "SHARE_ROOT", "exportLibrary")

# --- constructor provenance -------------------------------------------------
#
# The marker sweep above can only see a writer that NAMES a live path. It is
# blind to one that takes its path from an argument or a CLI flag, and totally
# blind to one handed an already-open handle. So sweep the other end instead:
# every site in apps/ that CONSTRUCTS a rekordbox DB handle must be either a
# mapped guard site or listed here with the reason it only reads.
#
# ``open_db`` is only counted in a file that references apps.shared.rekordbox_db
# (it is that module's opener; unrelated ``open_db`` names exist for state.db).
# ``sqlite3.connect`` is only counted when the argument is rekordbox-shaped and
# is not a ``mode=ro`` URI, which is proof of read-only on its face.
CONSTRUCTOR_PATTERNS: dict[str, re.Pattern[str]] = {
    "Rekordbox6Database(": re.compile(r"Rekordbox6Database\s*\("),
    "open_db(": re.compile(r"\b(?:rb_)?open_db\s*\("),
    "sqlite3.connect(": re.compile(
        r"sqlite3\.connect\s*\([^)\n]*(?:rb_db|rekordbox|master)[^)\n]*\)", re.IGNORECASE
    ),
}

READ_ONLY_DB_HANDLES: dict[str, str] = {
    "apps/shared/rekordbox_db.py": "IS the opener; no-arg default is the working copy",
    "apps/shared/library_integrity.py": "open_db(snapshot), SELECTs only",
    "apps/shared/state/ingest/rekordbox.py": "unlock=False + SELECTs, rekordbox -> state.db",
    "apps/audit/session_history.py": "open_db(REKORDBOX_LIVE_DB) rw handle, SELECTs only",
    "apps/audit/cue_comparison.py": "open_db() no-arg working copy, compare only",
    "apps/audit/match_rb_djay.py": "open_db() no-arg working copy, report only",
    "apps/audit/rekordbox_vs_music.py": "open_db() no-arg working copy, report only",
    "apps/audit/sync_diff.py": "open_db() no-arg working copy, diff only",
    "apps/launcher/scripts/bootstrap_db.py": "open_db() no-arg; bootstraps the working copy",
    "apps/open_dj/adapters/rekordbox.py": "open_db(--source) reads; writes an OpenDJ doc elsewhere",
    "apps/reconcile/heal_icloud_paths.py": "open_db() no-arg working copy",
    "apps/reconcile/list_broken.py": "open_db() no-arg working copy, report only",
    "apps/sync/playlist_diff.py": "rb_open_db(path) for a read-only diff",
    "apps/sync/usb/state.py": "open_db() no-arg working copy",
    "apps/tags/collect.py": "open_db() no-arg working copy, collects tags",
    "apps/analysis/backends/genre_hint.py": (
        "sqlite3.connect(<master.plain.db URI>?mode=ro, uri=True), one genre SELECT; "
        "the regex stops at the .resolve() paren before it sees mode=ro"
    ),
    "apps/sync/djay_sync_service.py": (
        "open_db() no-arg working copy in run_metadata_plan/run_cues_plan for audit "
        "diffs/plans; compare/plan only, writes CSVs under data/sync; live rekordbox "
        "writes are HTTP-guarded in routes/rb_djay_sync.py and guard_site on this module"
    ),
    "apps/webui/frontend/tests/e2e/support/vocals_demucs_fixture.py": (
        "e2e fixture builder: writes one vendor row only into its own disposable "
        "fixture dir's master.plain.db and refuses the canonical data/ copy or any "
        "path under ~/Library/Pioneer (_refuse_canonical_master); never a live handle"
    ),
}

# Writers keyed off an INJECTED handle. No constructor and no path, so neither
# sweep above can reach them; they are classified by hand and listed here so
# the classification is written down rather than assumed.
#
#   apps/sync/rb_writer.py write_cues -- IS a live-write path. Mapped as
#   module.sync.rb_writer.write_cues and guarded inside the function, which is
#   the only place a guard can go when the target is the argument.
INJECTED_HANDLE_WRITERS: dict[str, str] = {
    "apps/engine_core/store/schema.py": (
        "ensure_vendor_sidecar_tables(conn) runs DDL on a caller-supplied VENDOR "
        "connection. Its only production factory is rb_vendor -> _config."
        "MASTER_PLAIN_DB, i.e. data/master.plain.db, a working copy that is "
        "explicitly NOT gated. A caller that hands it a live connection would be "
        "a new write surface and needs a map entry"
    ),
    "apps/adapters/rekordbox/reversal.py": (
        "same injected vendor conn, same rb_vendor factory pinned to the plain "
        "working copy"
    ),
}

def _constructor_sites(path: Path, text: str) -> list[str]:
    """Rekordbox DB handle constructions in one file, by provenance not by path."""
    sites: list[str] = []
    for label, pattern in CONSTRUCTOR_PATTERNS.items():
        if label == "open_db(" and "rekordbox_db" not in text:
            continue  # someone else's open_db (state.db, dedup.db)
        for match in pattern.finditer(text):
            if label == "sqlite3.connect(" and "mode=ro" in match.group(0):
                continue  # read-only URI is proof on its face
            sites.append(f"{label}@{text[: match.start()].count(chr(10)) + 1}")
    return sites


def test_every_rekordbox_db_constructor_is_gated_or_allowlisted() -> None:
    """The structural guard: classify by who OPENS the DB, not who names it.

    GAP-2 and GAP-3 both existed because a writer can take its target as an
    argument (``--rb-db``) or as an already-open handle, and then never
    mentions a live path for a marker sweep to find. Sweeping constructors
    catches the argument shape; the handle shape has no constructor at all and
    is classified by hand in INJECTED_HANDLE_WRITERS.
    """
    guarded = {s.guard_site for s in WRITE_SURFACES}
    unclassified: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "apps").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative.startswith(DERIVED_BUILD_OUTPUT):
            continue
        if relative in guarded or relative in READ_ONLY_DB_HANDLES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        sites = _constructor_sites(path, text)
        if sites:
            unclassified[relative] = sites
    assert not unclassified, (
        "these modules construct a rekordbox DB handle but are neither a mapped "
        f"guard site nor allowlisted as read-only: {unclassified}. A handle can "
        "hold the LIVE master.db whether or not the file names it, so classify "
        "it: add a WriteSurface plus a guard, or an entry in "
        "READ_ONLY_DB_HANDLES with the reason it only reads."
    )


def test_constructor_allowlist_has_no_stale_entries() -> None:
    """An allowlist entry whose sites are gone is a reason nobody re-checked."""
    stale: list[str] = []
    for relative in READ_ONLY_DB_HANDLES:
        path = REPO_ROOT / relative
        if not path.is_file():
            stale.append(f"{relative} (file gone)")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _constructor_sites(path, text):
            stale.append(f"{relative} (no constructor left)")
    assert not stale, f"READ_ONLY_DB_HANDLES is stale: {stale}"


def test_injected_handle_writers_are_still_pinned_to_a_working_copy() -> None:
    """The handle-keyed sites neither sweep can see are still hand-classified."""
    missing = [
        relative for relative in INJECTED_HANDLE_WRITERS
        if not (REPO_ROOT / relative).is_file()
    ]
    assert not missing, f"INJECTED_HANDLE_WRITERS names files that are gone: {missing}"
    vendor_factory = (REPO_ROOT / "apps/webui/server/rb_vendor.py").read_text(
        encoding="utf-8"
    )
    assert "MASTER_PLAIN_DB" in vendor_factory, (
        "the rekordbox vendor writer factory no longer pins data/master.plain.db; "
        "ensure_vendor_sidecar_tables and the reversal DDL may now run against "
        "the LIVE database, which would make them write surfaces"
    )


def test_allowlist_has_no_stale_entries() -> None:
    missing = [
        relative for relative in IMPORT_DIRECTION_ONLY
        if not (REPO_ROOT / relative).is_file()
    ]
    assert not missing, f"IMPORT_DIRECTION_ONLY names files that no longer exist: {missing}"


# --- the suite may not aim at the real library ------------------------------


def _non_docstring_strings(tree: ast.Module) -> list[str]:
    """Every string a module could USE as a path, docstrings excluded.

    Docstrings are excluded on purpose: the containment rules being tested are
    about lexical path handling, and explaining them needs to quote paths in
    prose. What must not exist is an absolute path a test could actually open.

    f-strings are stitched back together from their literal parts, so
    ``f"/api/v1/x/{y}/apply"`` is judged as the one route it is rather than as
    a bare ``/apply`` fragment.
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    values: list[str] = []
    inside_fstring: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        parts: list[str] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                inside_fstring.add(id(piece))
                parts.append(piece.value)
            else:
                parts.append("{}")
        values.append("".join(parts))

    values.extend(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and id(node) not in inside_fstring
    )
    return values


@pytest.mark.parametrize("relative", GATE_SUITE)
def test_no_gate_test_can_name_a_real_library_location(relative: str) -> None:
    """Fixtures only. A test in this suite may never address the real library.

    The failure mode this exists to make impossible is mundane: a tired probe
    is handed a realistic-looking path instead of a fake one, the gate is
    briefly off for an unrelated reason, and the suite writes to the actual
    master.db. So every path a gate test builds must come from ``tmp_path``,
    which means no absolute path literals (routes aside) and no reading of the
    live constants -- those may be redirected with monkeypatch.setattr, which
    is how a lane probe stays off the real Pioneer share, but never read.
    """
    text = (REPO_ROOT / relative).read_text(encoding="utf-8")
    absolute = sorted(
        {
            value
            for value in _non_docstring_strings(ast.parse(text))
            # A bare "/" is a separator, not a target; "/api/..." is a route.
            if value.startswith("/") and len(value) > 1
            and not value.startswith("/api/")
        }
    )
    assert not absolute, (
        f"{relative} contains absolute path literals {absolute}; gate tests build "
        "every path from tmp_path so that no probe can address the real library"
    )
    for line_number, line in enumerate(text.splitlines(), start=1):
        if "monkeypatch.setattr" in line:
            continue  # redirecting a constant is the point; reading it is not
        if VOCABULARY_MARKER in line:
            continue  # the ban list itself, which this sweep also reads
        for banned in HOME_ESCAPES + LIVE_CONSTANTS:
            assert banned not in line, (
                f"{relative}:{line_number} reaches for {banned!r}. A gate test may "
                "redirect that constant with monkeypatch.setattr, never read it: "
                "resolving it is one keystroke away from opening it"
            )


def test_no_unmapped_module_names_a_live_rekordbox_target() -> None:
    """A new write path that nobody wrote into the map fails review here."""
    guarded = {surface.guard_site for surface in WRITE_SURFACES}
    unaccounted: list[str] = []
    for path in sorted((REPO_ROOT / "apps").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative.startswith(DERIVED_BUILD_OUTPUT):
            continue
        if relative in guarded or relative in IMPORT_DIRECTION_ONLY:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(marker in text for marker in LIVE_TARGET_MARKERS):
            unaccounted.append(relative)
    assert not unaccounted, (
        "these modules name a live rekordbox target but are neither a mapped "
        "guard site nor allowlisted as import-direction-only: "
        f"{unaccounted}. Add a WriteSurface (and a guard), or add an entry to "
        "IMPORT_DIRECTION_ONLY with the reason it only reads."
    )


