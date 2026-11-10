"""Generate ``reqs.json`` from ``.planning/REQUIREMENTS.md`` plus ``(private path)/``.

Usage::

    python -m scripts.build_reqs_json            # write reqs.json
    python -m scripts.build_reqs_json --check    # exit 1 if the sources do not build

The sources of truth are ``.planning/REQUIREMENTS.md`` and the per-id fragment
files in ``(private path)/`` (issue #10541). ``reqs.json`` lives at the
repo root so tests + CI can consume it without parsing markdown. It is a
gitignored build artifact, never committed: a committed aggregate re-conflicted
every open PR that added a requirement each time another one merged. Readers call
:func:`ensure_output` (pytest does it in the root ``conftest.py``), which rebuilds
it when it is missing or stale.

Fragment files (one requirement per file, named ``<ID>.md``)::

    ## v1 Requirements

    ### Library (LIBM)

    - [ ] **LIBM-10541** One requirement bullet, wrapped lines indented.

The ``##`` line names the section, the ``###`` line the category exactly as it
reads in REQUIREMENTS.md (a new category is appended to the end of the section).
The fragment is stitched into the REQUIREMENTS.md text before parsing, so every
rule below applies to it unchanged, duplicate ids included. Two PRs that each add
their own fragment touch different paths, so neither conflicts with the other.

Parsing rules (kept intentionally narrow — the ``.planning/REQUIREMENTS.md``
file has a stable shape):
  * ``## v1 Requirements`` - collects categories until ``## v1.1 Requirements``
    or ``## v2 Requirements`` (whichever comes first).
  * ``## v1.1 Requirements`` (optional) - same bullet shape as v1; collects until
    ``## v2 Requirements``.
  * ``## v2 Requirements`` - collects until ``## v3 Requirements`` / ``## Out of
    Scope`` / ``## Traceability``, whichever comes first.
  * ``## v3 Requirements`` (optional) - same free-text-category, no-checkbox-
    required bullet shape as v2 (a scope-move bucket, not a new release phase);
    collects until ``## Out of Scope`` / ``## Traceability``. Present in
    ``reqs.json`` only when the section exists, exactly like ``v1.1``. A v3 id
    counts toward the duplicate-id check and ``_all_ids`` but never toward v1
    totals or burndown.
  * ``## Out of Scope`` — reads the markdown table rows.
  * A category header is ``### <Name> (CODE)``; bullets beneath it matching
    ``- [x] **RECON-01** ...`` or ``- [ ] **RECON-01** ...`` / ``- **CROSS-01**``
    become requirements. ``[x]`` = shipped, ``[ ]`` = pending, no-checkbox = pending.
  * ``(shipped Phase N)`` inline tags populate ``shipped_phase``.
  * Indented non-bullet, non-stopper lines after a requirement bullet join
    ``desc`` (wrapped descriptions; issue #1683).
  * The traceability table feeds each requirement's ``phase`` field.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
SOURCE: Path = REPO_ROOT / ".planning" / "REQUIREMENTS.md"
REQS_MD_REL: str = ".planning/REQUIREMENTS.md"
OUTPUT: Path = REPO_ROOT / "reqs.json"
COVERAGE_MATRIX_TEMPLATE: Path = REPO_ROOT / "coverage-matrix.md"
DEEPEN_HINT: str = "git fetch --deepen=900 origin main"
#: Fragment directory, relative to the repo root. The live path is always derived
#: from ``SOURCE`` (see :func:`_fragments_dir`) so a caller that points ``SOURCE`` at
#: another checkout reads that checkout's fragments, never this one's.
FRAGMENTS_REL = "(private path)"

# ------------------------------------------------------------------ parser

# A category code may carry more than one segment (SYNC-ONEWAY), so it is one
# or more all-caps segments rather than a single run of letters. A bare RECON
# still matches, so every pre-existing ID parses exactly as it did before.
# This widened after SYNC-ONEWAY-01..04 parsed as nothing at all: the
# requirement count simply did not move, which is a silent miss, not an error.
# Each segment can also mix in digits after its leading letter (A11Y), which
# widened it a second time after A11Y-01/A11Y-02 parsed as nothing at all --
# same silent-miss shape, caught only by grepping reqs.json by hand.
# Wrapped continuation lines used to truncate ``desc`` with no error (issue
# #1683), same silent-miss class as those two.
_CODE = r"[A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*"
# A full requirement id (e.g. "A11Y-01", "SYNC-ONEWAY-04c"): a category code
# plus its numeric suffix. Every place in this file that recognizes a
# requirement id token -- bullets, and the Traceability table -- must build
# off this one pattern rather than hand-duplicating a letters-only variant,
# which is exactly how the Traceability table kept silently dropping
# alphanumeric ids (A11Y-01 etc.) after _CODE was widened for the bullet
# parsers but the table's own regex was not.
_REQ_ID = rf"{_CODE}-(?:\d+[a-z]?|S\d+)"
_CATEGORY_RE = re.compile(rf"^###\s+(.+?)\s+\(({_CODE})\)\s*$")
_BULLET_V1_RE = re.compile(rf"^-\s+(?:\[( |x)\]\s+)?\*\*({_REQ_ID})\*\*(.*)$")
# v2 bullets often lack a checkbox (`- **CROSS-01**: Linux support`), but a
# shipped one carries the same `[x]` marker v1 uses (`- [x] **DEVLOOP-01**: ...`).
_BULLET_V2_RE = re.compile(
    rf"^-\s+(?:\[( |x)\]\s+)?\*\*({_REQ_ID})\*\*\s*:?\s*(.*)$"
)
_SHIPPED_PHASE_RE = re.compile(r"\(shipped\s+(Phase\s+[\w.]+)\)", re.IGNORECASE)
_CONTINUATION_STOP_PREFIXES = ("-", "#", ">", "<!--", "|")


def ids_from_requirements_text(text: str) -> set[str]:
    """Bullet-defined requirement ids in markdown text (v1 and v2 shapes)."""
    ids: set[str] = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        m_b = _BULLET_V2_RE.match(stripped) or _BULLET_V1_RE.match(stripped)
        if m_b:
            ids.add(m_b.group(2))
    return ids


def _is_description_continuation(line: str) -> bool:
    if line.strip() == "":
        return False
    if not line[:1].isspace():
        return False
    stripped = line.lstrip()
    return not stripped.startswith(_CONTINUATION_STOP_PREFIXES)


def _continuation_text(lines: list[str], bullet_index: int) -> str:
    parts: list[str] = []
    j = bullet_index + 1
    while j < len(lines) and _is_description_continuation(lines[j]):
        piece = lines[j].strip()
        if piece:
            parts.append(piece)
        j += 1
    return " ".join(parts)


def _read_lines(src: Path) -> list[str]:
    return src.read_text(encoding="utf-8").splitlines()


# ------------------------------------------------------------------ fragments

_SECTION_RE = re.compile(r"^##\s+(v[\d.]+ Requirements)\s*$")


def _fragments_dir() -> Path:
    return SOURCE.parent / Path(FRAGMENTS_REL).name


def fragment_paths(directory: Path) -> list[Path]:
    """Every fragment file in ``directory`` in stable order (a README is not a fragment)."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.md") if p.name != "README.md")


def _fragment_parts_from_text(text: str, *, stem: str, label: str) -> tuple[str, str, list[str]]:
    """Return (section, category header line, body lines) of one fragment, failing on any other shape."""
    lines = text.splitlines()
    heads = [i for i, line in enumerate(lines) if line.strip()]
    if len(heads) < 3:
        raise ValueError(f"{label}: a fragment needs a '## vN Requirements' line, a '### ' category and one bullet")
    section_m = _SECTION_RE.match(lines[heads[0]].strip())
    category = lines[heads[1]].strip()
    if section_m is None:
        raise ValueError(
            f"{label}: first line must be '## v1 Requirements' (or v1.1, v2, v3), got {lines[heads[0]]!r}"
        )
    if not category.startswith("### "):
        raise ValueError(f"{label}: second line must be a '### Category (CODE)' header, got {category!r}")
    body = lines[heads[1] + 1 :]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    ids = ids_from_requirements_text("\n".join(body))
    if ids != {stem}:
        raise ValueError(f"{label}: must define exactly the id {stem!r} (its file name), defines {sorted(ids)}")
    if any(line.startswith("#") for line in body):
        raise ValueError(f"{label}: one section and one category per fragment")
    return section_m.group(1), category, body


def _fragment_parts(path: Path) -> tuple[str, str, list[str]]:
    return _fragment_parts_from_text(path.read_text(encoding="utf-8"), stem=path.stem, label=str(path))


def fragments_text_at(git: str, rev: str) -> tuple[str | None, str]:
    """Every fragment's text at ``rev`` joined by newlines ("" for none), or None plus git's stderr.

    For tools that read the requirement ids of a commit rather than a checkout
    (``scripts.req_id_pr_number``, ``scripts.next_requirement_id``).
    """
    listing = subprocess.run(
        [git, "ls-tree", "-r", "--name-only", rev, "--", f"{FRAGMENTS_REL}/"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if listing.returncode != 0:
        return None, f"git ls-tree {rev} exited {listing.returncode}: {listing.stderr.strip()}"
    texts: list[str] = []
    for rel in sorted(listing.stdout.splitlines()):
        if not rel.endswith(".md") or rel.endswith("/README.md"):
            continue
        shown = subprocess.run(
            [git, "show", f"{rev}:{rel}"], capture_output=True, text=True, encoding="utf-8", check=False
        )
        if shown.returncode != 0:
            return None, f"git show {rev}:{rel} exited {shown.returncode}: {shown.stderr.strip()}"
        texts.append(shown.stdout)
    return "\n".join(texts), ""


def fragments_text_in(directory: Path) -> str:
    """Every fragment's text in ``directory`` joined by newlines ("" for none)."""
    return "\n".join(p.read_text(encoding="utf-8") for p in fragment_paths(directory))


def _insert_fragment(lines: list[str], path: Path) -> list[str]:
    section, category, body = _fragment_parts(path)
    return _insert_fragment_parts(lines, section, category, body, label=str(path))


def _insert_fragment_parts(lines: list[str], section: str, category: str, body: list[str], *, label: str) -> list[str]:
    start, end = _section_bounds(lines, section)
    if start < 0:
        raise ValueError(f"{label}: {REQS_MD_REL} has no '## {section}' section to stitch into")
    cat_idx = next((i for i in range(start, end) if lines[i].strip() == category), None)
    if cat_idx is None:
        block, region_start, region_end = ["", category, "", *body], start, end
    else:
        block, region_start = body, cat_idx + 1
        region_end = next((i for i in range(region_start, end) if lines[i].startswith("#")), end)
    insert_at = region_end
    while insert_at > region_start and not lines[insert_at - 1].strip():
        insert_at -= 1
    return [*lines[:insert_at], *block, *lines[insert_at:]]


def stitched_lines() -> list[str]:
    """REQUIREMENTS.md lines with every fragment stitched into its section and category."""
    if not SOURCE.exists():
        raise FileNotFoundError(f"Cannot read requirements source: {SOURCE}")
    lines = _read_lines(SOURCE)
    for path in fragment_paths(_fragments_dir()):
        lines = _insert_fragment(lines, path)
    return lines


def _commit_tree(repo_root: Path, commit_sha: str) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", f"{commit_sha}^{{tree}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"commit {commit_sha[:12]} is not present locally. Run: {DEEPEN_HINT}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _blob_at_tree(repo_root: Path, tree_sha: str, rel: str) -> str:
    proc = subprocess.run(
        ["git", "show", f"{tree_sha}:{rel}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise FileNotFoundError(f"{rel} is absent in merge tree {tree_sha[:12]}: {proc.stderr.strip()}")
    return proc.stdout


def _fragment_paths_at_tree(repo_root: Path, tree_sha: str) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", tree_sha, "--", f"{FRAGMENTS_REL}/"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-tree {tree_sha[:12]}:{FRAGMENTS_REL}/ failed: {proc.stderr.strip()}")
    return sorted(
        rel
        for rel in proc.stdout.splitlines()
        if rel.endswith(".md") and not rel.endswith("/README.md")
    )


def stitched_lines_at_tree(repo_root: Path, tree_sha: str) -> list[str]:
    """Ledger lines after stitching every fragment file present in ``tree_sha``."""
    lines = _blob_at_tree(repo_root, tree_sha, REQS_MD_REL).splitlines()
    for rel in _fragment_paths_at_tree(repo_root, tree_sha):
        stem = Path(rel).name.removesuffix(".md")
        text = _blob_at_tree(repo_root, tree_sha, rel)
        section, category, body = _fragment_parts_from_text(text, stem=stem, label=rel)
        lines = _insert_fragment_parts(lines, section, category, body, label=rel)
    return lines


def stitched_lines_at_commit(repo_root: Path, commit_sha: str) -> list[str]:
    return stitched_lines_at_tree(repo_root, _commit_tree(repo_root, commit_sha))


def merged_tree_sha(base_sha: str, head_sha: str, repo_root: Path) -> str:
    for sha in (base_sha, head_sha):
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if probe.returncode != 0:
            raise RuntimeError(f"commit {sha[:12]} is not present locally. Run: {DEEPEN_HINT}")
    proc = subprocess.run(
        ["git", "merge-tree", "--write-tree", base_sha, head_sha],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode > 1:
        raise RuntimeError(
            f"git merge-tree --write-tree failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    tree = proc.stdout.splitlines()[0].strip() if proc.stdout else ""
    if not tree:
        raise RuntimeError("git merge-tree --write-tree printed no tree oid")
    return tree


def _rev_parse(ref: str, repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git rev-parse {ref} failed rc={proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _split_merged_duplicates(
    base_dups: dict[str, int], merged_dups: dict[str, int]
) -> tuple[dict[str, int], dict[str, int]]:
    """Partition merged duplicate ids into main pre-existing vs introduced by the merge."""
    preexisting: dict[str, int] = {}
    introduced: dict[str, int] = {}
    for rid, merged_count in merged_dups.items():
        if rid in base_dups:
            preexisting[rid] = merged_count
        else:
            introduced[rid] = merged_count
    return preexisting, introduced


def run_merged_with_check(base_ref: str, head_ref: str, repo_root: Path) -> int:
    """Fail when ``head_ref`` merged with ``base_ref`` would define any requirement id twice."""
    try:
        base_sha = _rev_parse(base_ref, repo_root)
        head_sha = _rev_parse(head_ref, repo_root)
        merged_tree = merged_tree_sha(base_sha, head_sha, repo_root)
        merged_lines = stitched_lines_at_tree(repo_root, merged_tree)
        merged_dups = duplicate_ids_in_lines(merged_lines)
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"[reqs-merge] UNKNOWN: {exc}", file=sys.stderr)
        return 2
    if not merged_dups:
        print(f"[reqs-merge] OK -- no duplicate ids after merge with {base_ref}.")
        return 0
    base_lines = stitched_lines_at_commit(repo_root, base_sha)
    base_dups = duplicate_ids_in_lines(base_lines)
    preexisting, introduced = _split_merged_duplicates(base_dups, merged_dups)
    all_listed = ", ".join(f"{rid} x{n}" for rid, n in sorted(merged_dups.items()))
    print(
        f"[reqs-merge] merged {REQS_MD_REL} (with {FRAGMENTS_REL}/) defines "
        f"{len(merged_dups)} id(s) more than once: {all_listed}.",
        file=sys.stderr,
    )
    if preexisting:
        listed = ", ".join(f"{rid} x{n}" for rid, n in sorted(preexisting.items()))
        print(
            f"[reqs-merge] Pre-existing on {base_ref} (repair on main, not this PR): {listed}",
            file=sys.stderr,
        )
    if introduced:
        listed = ", ".join(f"{rid} x{n}" for rid, n in sorted(introduced.items()))
        print(f"[reqs-merge] Introduced by this merge: {listed}", file=sys.stderr)
    return 1


def _section_bounds(lines: list[str], header: str) -> tuple[int, int]:
    """Return (start_line_after_header, end_line_exclusive) for ``## header``.

    End is the next ``## `` line or EOF.
    """
    start = -1
    for i, line in enumerate(lines):
        if line.strip() == f"## {header}":
            start = i + 1
            break
    if start < 0:
        return -1, -1
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## ") and not lines[j].startswith("### "):
            end = j
            break
    return start, end


def _parse_versioned_requirements(lines: list[str], header: str) -> dict:
    start, end = _section_bounds(lines, header)
    if start < 0:
        return {}
    categories: dict[str, dict] = {}
    current_code: str | None = None
    for i in range(start, end):
        line = lines[i].rstrip()
        m_cat = _CATEGORY_RE.match(line)
        if m_cat:
            name, code = m_cat.group(1).strip(), m_cat.group(2).strip()
            current_code = code
            # setdefault, not assignment: two "### <Name> (CODE)" headers can
            # legitimately share one CODE (e.g. "Admin operator surfaces
            # (ADMIN)" appearing twice, once per shipped PR). A plain
            # `categories[code] = ...` replaces the dict object the first
            # header built, silently dropping every requirement already
            # collected under it -- same silent-miss shape as the _CODE
            # widenings above, just one level up. v2's parser already uses
            # setdefault for this reason.
            existing = categories.setdefault(code, {"name": name, "requirements": []})
            # A second header reusing CODE with a DIFFERENT name is not the
            # supported case above -- it is indistinguishable from a typo'd
            # code on what was meant to be its own category, and setdefault
            # would silently keep only the first header's name with no
            # signal (issue found on PR #2803: reqs.json filed the
            # Diagnostics-tab requirement under "Admin operator surfaces"
            # with no error). Fail fast instead of guessing which name is
            # right.
            if existing["name"] != name:
                raise ValueError(
                    f"{SOURCE}: two '### <Name> ({code})' headers share code "
                    f"{code!r} but disagree on name ({existing['name']!r} vs "
                    f"{name!r}). Either they are the same category and the "
                    f"names must match verbatim, or one of them needs its "
                    f"own code."
                )
            continue
        if current_code is None:
            continue
        m_b = _BULLET_V1_RE.match(line.lstrip())
        if not m_b:
            continue
        check, rid, rest = m_b.group(1), m_b.group(2), m_b.group(3)
        status = "shipped" if check == "x" else "pending"
        shipped_phase: str | None = None
        sm = _SHIPPED_PHASE_RE.search(rest)
        if sm:
            shipped_phase = sm.group(1)
        # Strip leading "(shipped …):" / ":" and return the rest as description.
        cont = _continuation_text(lines, i)
        desc = f"{rest} {cont}" if cont else rest
        desc = _SHIPPED_PHASE_RE.sub("", desc).strip()
        desc = desc.lstrip(":").strip()
        # Also drop a leading paren-tag like "(shipped ...)" leftovers and
        # collapse whitespace.
        desc = re.sub(r"\s+", " ", desc)
        categories[current_code]["requirements"].append(
            {
                "id": rid,
                "desc": desc,
                "status": status,
                "phase": None,  # filled later from traceability table
                "shipped_phase": shipped_phase,
            }
        )
    return categories


def _parse_v1(lines: list[str]) -> dict:
    return _parse_versioned_requirements(lines, "v1 Requirements")


# v2 (and v3, which reuses this shape) categories often lack a parenthesized
# code: `### Cross-Platform` with bullets like `- **CROSS-01**: ...`. We infer
# the code from the first ID.
_CATEGORY_V2_RE = re.compile(r"^###\s+(.+?)\s*$")


def _parse_v2_style(lines: list[str], header: str) -> dict:
    """Parse a ``## <header>`` section using the free-text-category, optional-
    checkbox bullet shape shared by v2 and v3. Returns ``{}`` when the section
    is absent; callers distinguish "absent" from "present but empty" the same
    way ``v1.1`` does, by checking ``_section_bounds`` themselves first.
    """
    start, end = _section_bounds(lines, header)
    if start < 0:
        return {}
    categories: dict[str, dict] = {}
    current_name: str | None = None
    current_code: str | None = None
    for i in range(start, end):
        line = lines[i].rstrip()
        # Try the strict `(CODE)` form first, then fall back to free-text.
        m_cat = _CATEGORY_RE.match(line)
        if m_cat:
            current_name = m_cat.group(1).strip()
            current_code = m_cat.group(2).strip()
            categories.setdefault(current_code, {"name": current_name, "requirements": []})
            continue
        m_cat_free = _CATEGORY_V2_RE.match(line)
        if m_cat_free:
            current_name = m_cat_free.group(1).strip()
            current_code = None  # defer until we see a bullet
            continue
        if current_name is None:
            continue
        m_b = _BULLET_V2_RE.match(line.lstrip())
        if not m_b:
            continue
        check, rid, rest = m_b.group(1), m_b.group(2), m_b.group(3).strip()
        status = "shipped" if check == "x" else "pending"
        cont = _continuation_text(lines, i)
        desc = f"{rest} {cont}" if cont else rest
        # Infer code from the bullet's prefix if the heading was free-text.
        if current_code is None:
            current_code = rid.split("-", 1)[0]
            categories.setdefault(current_code, {"name": current_name, "requirements": []})
        categories[current_code]["requirements"].append(
            {
                "id": rid,
                "desc": re.sub(r"\s+", " ", desc),
                "status": status,
                "phase": None,
                "shipped_phase": None,
            }
        )
    return categories


def _parse_v2(lines: list[str]) -> dict:
    return _parse_v2_style(lines, "v2 Requirements")


def _parse_v3(lines: list[str]) -> dict:
    return _parse_v2_style(lines, "v3 Requirements")


def _parse_out_of_scope(lines: list[str]) -> list[dict]:
    start, end = _section_bounds(lines, "Out of Scope")
    if start < 0:
        return []
    rows: list[dict] = []
    for i in range(start, end):
        line = lines[i].rstrip()
        if not line.startswith("|"):
            continue
        # Skip the header + separator rows.
        if "---" in line or line.lower().startswith("| feature"):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 2 or not parts[0] or parts[0].lower() == "feature":
            continue
        rows.append({"feature": parts[0], "reason": parts[1]})
    return rows


def _parse_traceability(lines: list[str]) -> dict[str, str]:
    """Return ``{requirement_id: phase}`` from the Traceability table.

    Expands pattern rows like ``META-*`` and ``CAT-01..03`` into every
    matching literal ID; ambiguous pattern rows are skipped.
    """
    start, end = _section_bounds(lines, "Traceability")
    if start < 0:
        return {}
    mapping: dict[str, str] = {}
    for i in range(start, end):
        line = lines[i].rstrip()
        if not line.startswith("|"):
            continue
        if "---" in line or line.lower().startswith("| requirement"):
            continue
        # Columns: | Requirement | Phase | Status |
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 2:
            continue
        req_spec, phase = parts[0], parts[1]
        if not req_spec or req_spec.lower() == "requirement":
            continue
        # Skip glob patterns — the literal-bullet parser already catches
        # the individual IDs; we only need exact matches here.
        if "*" in req_spec or ".." in req_spec:
            continue
        for token in (t.strip() for t in req_spec.split(",")):
            if re.fullmatch(_REQ_ID, token):
                mapping[token] = phase
    return mapping


# ------------------------------------------------------------------ build


def _present_buckets_from_lines(lines: list[str]) -> tuple[dict, ...]:
    v1 = _parse_v1(lines)
    v2 = _parse_v2(lines)
    v1_1_start, _ = _section_bounds(lines, "v1.1 Requirements")
    v1_1 = (
        _parse_versioned_requirements(lines, "v1.1 Requirements")
        if v1_1_start >= 0
        else None
    )
    v3_start, _ = _section_bounds(lines, "v3 Requirements")
    v3 = _parse_v3(lines) if v3_start >= 0 else None
    return tuple(bucket for bucket in (v1, v1_1, v2, v3) if bucket is not None)


def duplicate_ids_in_lines(lines: list[str]) -> dict[str, int]:
    """Return requirement ids defined more than once in markdown lines."""
    return _duplicate_ids(*_present_buckets_from_lines(lines))


def _duplicate_ids(*buckets: dict) -> dict[str, int]:
    """Requirement ids that ``REQUIREMENTS.md`` defines more than once.

    Nothing else in the pipeline notices these. The parser keeps both copies,
    so ``reqs.json`` faithfully reflects a source file that defines one id
    twice, and both existing gates stay green: ``--check`` compares the file
    with the source, and the "regenerating must produce no diff" step compares
    regeneration with itself. Measured Fri 4 Sep 2026 on a planted duplicate of
    RECON-01: 211 ids -> 212, still 211 distinct, both gates green.

    This matters because ``.planning/REQUIREMENTS.md`` is an append-only ledger
    that 11 open PRs conflict on, and every proposed fix for that (a union
    merge driver above all) works by keeping BOTH sides of an append. Keeping
    both sides is right for text and wrong for identifiers, so the id space
    needs its own gate before that policy is safe to adopt.
    """
    counts: collections.Counter[str] = collections.Counter()
    for bucket in buckets:
        for category in bucket.values():
            for requirement in category["requirements"]:
                counts[requirement["id"]] += 1
    return {rid: n for rid, n in counts.items() if n > 1}


def _build_payload() -> dict:
    lines = stitched_lines()
    v1 = _parse_v1(lines)
    v2 = _parse_v2(lines)
    v1_1_start, _ = _section_bounds(lines, "v1.1 Requirements")
    v1_1 = (
        _parse_versioned_requirements(lines, "v1.1 Requirements")
        if v1_1_start >= 0
        else None
    )
    v3_start, _ = _section_bounds(lines, "v3 Requirements")
    v3 = _parse_v3(lines) if v3_start >= 0 else None

    present_buckets: tuple[dict, ...] = tuple(
        bucket
        for bucket in (v1, v1_1, v2, v3)
        if bucket is not None
    )
    duplicates = _duplicate_ids(*present_buckets)
    if duplicates:
        listed = ", ".join(f"{rid} x{n}" for rid, n in sorted(duplicates.items()))
        raise ValueError(
            f"{SOURCE} (with {FRAGMENTS_REL}/) defines {len(duplicates)} requirement id(s) more than "
            f"once: {listed}. Two branches appending the same id is the normal "
            f"way this happens; renumber one side rather than deleting either, "
            f"since a traceability row may already point at it."
        )
    trace = _parse_traceability(lines)
    oos = _parse_out_of_scope(lines)

    # Attach phase mapping.
    for bucket in present_buckets:
        for cat in bucket.values():
            for req in cat["requirements"]:
                if req["id"] in trace:
                    req["phase"] = trace[req["id"]]

    # Render source as a repo-relative path when possible, absolute otherwise
    # (so tests can monkeypatch SOURCE outside the repo root without crashing).
    try:
        # as_posix(): the serialized source key must be platform-stable so
        # reqs.json round-trips identically on Windows checkouts.
        source_str = SOURCE.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        source_str = str(SOURCE)

    payload: dict = {
        # No generated_at field: a timestamp (even one seeded from mtime) makes
        # reqs.json drift on every git checkout, which flakes the "in sync"
        # test. The content is fully derivable from REQUIREMENTS.md, so the
        # source path + parsed data is enough.
        "source": source_str,
        "v1": v1,
        "v2": v2,
        "out_of_scope": oos,
    }
    if v1_1 is not None:
        payload["v1.1"] = v1_1
    if v3 is not None:
        payload["v3"] = v3
    return payload


def _all_ids(payload: dict) -> list[str]:
    ids: list[str] = []
    bucket_names = ["v1"]
    if "v1.1" in payload:
        bucket_names.append("v1.1")
    bucket_names.append("v2")
    if "v3" in payload:
        bucket_names.append("v3")
    for bucket in bucket_names:
        for cat in payload.get(bucket, {}).values():
            for req in cat.get("requirements", []):
                ids.append(req["id"])
    return ids


def _write_matrix_template() -> None:
    """Write a placeholder ``coverage-matrix.md`` if one doesn't exist yet.

    The real one is generated by the pytest plugin; this just gives users a
    file to inspect before they run tests. The file is gitignored (it is a
    build artifact), so a fresh clone has none until the suite runs -- hence
    the placeholder says how to populate it rather than showing an empty
    table that could be mistaken for zero coverage.
    """
    if COVERAGE_MATRIX_TEMPLATE.exists():
        return
    header = [
        "# Coverage Matrix\n",
        "\n",
        (
            "_Placeholder -- not yet generated. This file is a build artifact "
            "(gitignored), rewritten by `scripts/pytest_reqs_plugin.py` on "
            "every pytest run._\n"
        ),
        "\n",
        (
            "**No run yet -- this is not a coverage figure.** Populate it "
            "with a full run:\n"
        ),
        "\n",
        "```bash\n",
        "make test\n",
        "```\n",
        "\n",
        (
            "A narrowed run (`-k`, `-m`, an explicit path) produces figures "
            "scoped to that run; the generated header stamps those as PARTIAL "
            "RUN. CI publishes the full-suite copy as the `coverage-matrix` "
            "artifact.\n"
        ),
    ]
    COVERAGE_MATRIX_TEMPLATE.write_text("".join(header), encoding="utf-8")


def _serialize(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _write_text_atomic(path: Path, text: str) -> None:
    # Atomic: pytest-xdist workers and the controller all call ensure_output at once.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_output(payload: dict) -> None:
    _write_text_atomic(OUTPUT, _serialize(payload))


def ensure_output() -> Path:
    """Rebuild ``reqs.json`` when it is missing or stale and return its path.

    ``reqs.json`` is a gitignored build artifact (issue #10541), so every reader
    that wants the file calls this first. Raises on sources that do not build.
    """
    serialized = _serialize(_build_payload())
    if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != serialized:
        _write_text_atomic(OUTPUT, serialized)
    return OUTPUT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.build_reqs_json",
        description="Generate reqs.json from .planning/REQUIREMENTS.md and (private path)/",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if REQUIREMENTS.md and requirements.d/ do not build.",
    )
    parser.add_argument(
        "--merged-with",
        metavar="REF",
        default=None,
        help=(
            "Exit non-zero when REF merged with HEAD would define any requirement id more "
            "than once (union-merge duplicate gate; issue #3009)."
        ),
    )
    parser.add_argument(
        "--merge-head",
        default="HEAD",
        help="Commit to merge with --merged-with (default: HEAD).",
    )
    args = parser.parse_args(argv)

    if args.merged_with:
        merge_code = run_merged_with_check(args.merged_with, args.merge_head, REPO_ROOT)
        if merge_code != 0:
            return merge_code
        if not args.check:
            return 0

    payload = _build_payload()

    if args.check:
        # reqs.json is no longer committed (issue #10541), so there is no on-disk
        # copy to drift: the check is that the sources build (no duplicate ids, no
        # malformed fragment). A build failure raises with the offending path.
        print(f"[reqs-check] OK -- {len(_all_ids(payload))} requirements build from the sources.")
        return 0

    _write_output(payload)
    _write_matrix_template()
    try:
        shown = OUTPUT.relative_to(REPO_ROOT)
    except ValueError:
        shown = OUTPUT
    print(f"Wrote {shown} ({len(_all_ids(payload))} requirements)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
