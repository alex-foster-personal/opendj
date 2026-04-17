"""Generate ``reqs.json`` from ``.planning/REQUIREMENTS.md``.

Usage::

    python -m scripts.build_reqs_json            # write reqs.json
    python -m scripts.build_reqs_json --check    # exit 1 if reqs.json drifted

The source of truth is ``.planning/REQUIREMENTS.md``. ``reqs.json`` lives at
the repo root so tests + CI can consume it without parsing markdown.

Parsing rules (kept intentionally narrow — the ``.planning/REQUIREMENTS.md``
file has a stable shape):
  * ``## v1 Requirements`` — collects categories until ``## v2 Requirements``.
  * ``## v2 Requirements`` — collects until ``## Out of Scope`` / ``## Traceability``.
  * ``## Out of Scope`` — reads the markdown table rows.
  * A category header is ``### <Name> (CODE)``; bullets beneath it matching
    ``- [x] **RECON-01** ...`` or ``- [ ] **RECON-01** ...`` / ``- **CROSS-01**``
    become requirements. ``[x]`` = shipped, ``[ ]`` = pending, no-checkbox = pending.
  * ``(shipped Phase N)`` inline tags populate ``shipped_phase``.
  * The traceability table feeds each requirement's ``phase`` field.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
SOURCE: Path = REPO_ROOT / ".planning" / "REQUIREMENTS.md"
OUTPUT: Path = REPO_ROOT / "reqs.json"
COVERAGE_MATRIX_TEMPLATE: Path = REPO_ROOT / "coverage-matrix.md"

# ------------------------------------------------------------------ parser

_CATEGORY_RE = re.compile(r"^###\s+(.+?)\s+\(([A-Z]+)\)\s*$")
_BULLET_V1_RE = re.compile(
    r"^-\s+(?:\[( |x)\]\s+)?\*\*([A-Z]+-\d+[a-z]?)\*\*(.*)$"
)
# v2 bullets often lack a checkbox: `- **CROSS-01**: Linux support`
_BULLET_V2_RE = re.compile(r"^-\s+\*\*([A-Z]+-\d+[a-z]?)\*\*\s*:?\s*(.*)$")
_SHIPPED_PHASE_RE = re.compile(r"\(shipped\s+(Phase\s+[\w.]+)\)", re.IGNORECASE)
_TRACE_ROW_RE = re.compile(
    r"^\|\s*([A-Z]+-\d+|[A-Z]+-\*|[A-Z]+-[\d.]+(?:\.\.\d+)?(?:,\s*[A-Z]+-\d+)*)"
    r"\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$"
)


def _read_lines(src: Path) -> list[str]:
    return src.read_text(encoding="utf-8").splitlines()


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


def _parse_v1(lines: list[str]) -> dict:
    start, end = _section_bounds(lines, "v1 Requirements")
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
            categories[code] = {"name": name, "requirements": []}
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
        desc = rest
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


# v2 categories often lack a parenthesized code: `### Cross-Platform` with
# bullets like `- **CROSS-01**: ...`. We infer the code from the first ID.
_CATEGORY_V2_RE = re.compile(r"^###\s+(.+?)\s*$")


def _parse_v2(lines: list[str]) -> dict:
    start, end = _section_bounds(lines, "v2 Requirements")
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
        rid, desc = m_b.group(1), m_b.group(2).strip()
        # Infer code from the bullet's prefix if the heading was free-text.
        if current_code is None:
            current_code = rid.split("-", 1)[0]
            categories.setdefault(current_code, {"name": current_name, "requirements": []})
        categories[current_code]["requirements"].append(
            {
                "id": rid,
                "desc": re.sub(r"\s+", " ", desc),
                "status": "pending",
                "phase": None,
                "shipped_phase": None,
            }
        )
    return categories


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
            if re.fullmatch(r"[A-Z]+-\d+[a-z]?", token):
                mapping[token] = phase
    return mapping


# ------------------------------------------------------------------ build


def _build_payload() -> dict:
    if not SOURCE.exists():
        raise FileNotFoundError(f"Cannot read requirements source: {SOURCE}")
    lines = _read_lines(SOURCE)
    v1 = _parse_v1(lines)
    v2 = _parse_v2(lines)
    trace = _parse_traceability(lines)
    oos = _parse_out_of_scope(lines)

    # Attach phase mapping.
    for bucket in (v1, v2):
        for cat in bucket.values():
            for req in cat["requirements"]:
                if req["id"] in trace:
                    req["phase"] = trace[req["id"]]

    # Render source as a repo-relative path when possible, absolute otherwise
    # (so tests can monkeypatch SOURCE outside the repo root without crashing).
    try:
        source_str = str(SOURCE.relative_to(REPO_ROOT))
    except ValueError:
        source_str = str(SOURCE)

    payload = {
        # No generated_at field: a timestamp (even one seeded from mtime) makes
        # reqs.json drift on every git checkout, which flakes the "in sync"
        # test. The content is fully derivable from REQUIREMENTS.md, so the
        # source path + parsed data is enough.
        "source": source_str,
        "v1": v1,
        "v2": v2,
        "out_of_scope": oos,
    }
    return payload


def _all_ids(payload: dict) -> list[str]:
    ids: list[str] = []
    for bucket in ("v1", "v2"):
        for cat in payload.get(bucket, {}).values():
            for req in cat.get("requirements", []):
                ids.append(req["id"])
    return ids


def _write_matrix_template() -> None:
    """Write an empty ``coverage-matrix.md`` if one doesn't exist yet.

    The real one is generated by the pytest plugin; this just gives users a
    file to inspect before they run tests.
    """
    if COVERAGE_MATRIX_TEMPLATE.exists():
        return
    header = [
        "# Coverage Matrix\n",
        "\n",
        "_Template — regenerated by the pytest plugin on every run._\n",
        "\n",
        "| Requirement ID | Description | Covered by | Status |\n",
        "|---|---|---|---|\n",
        "| (run `make test` to populate) |  |  |  |\n",
    ]
    COVERAGE_MATRIX_TEMPLATE.write_text("".join(header), encoding="utf-8")


def _serialize(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _write_output(payload: dict) -> None:
    OUTPUT.write_text(_serialize(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.build_reqs_json",
        description="Generate reqs.json from .planning/REQUIREMENTS.md",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if reqs.json on disk differs.",
    )
    args = parser.parse_args(argv)

    payload = _build_payload()
    serialized = _serialize(payload)

    if args.check:
        if not OUTPUT.exists():
            print(f"[reqs-check] {OUTPUT} is missing. Run without --check.", file=sys.stderr)
            return 1
        on_disk = OUTPUT.read_text(encoding="utf-8")
        if on_disk != serialized:
            try:
                src_shown = SOURCE.relative_to(REPO_ROOT)
            except ValueError:
                src_shown = SOURCE
            print(
                f"[reqs-check] {OUTPUT.name} is out of sync with "
                f"{src_shown}. Run "
                "`python -m scripts.build_reqs_json` and commit.",
                file=sys.stderr,
            )
            return 1
        print(f"[reqs-check] OK — {len(_all_ids(payload))} requirements in sync.")
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
