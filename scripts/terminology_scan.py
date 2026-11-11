#!/usr/bin/env python3
"""Harvest domain jargon, rank it, and write (private path).

Rank is frequency times ambiguity times blast radius. Buckets: defined (with path),
load-bearing and undefined, or inconsistent across files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REPO = Path(__file__).resolve().parent.parent
REPORT_REL = Path("(private path)")
GLOSSARY_REL = Path("docs/glossary.md")
SCALAR_REL = Path("(private path)")
CURSOR_SKILLS_REL = Path("(private path)")

SEED_TERMS: tuple[str, ...] = (
    "Camelot",
    "phrase",
    "PSSI",
    "PVDI",
    "PQTZ",
    "ANLZ",
    "TSAF",
    "stable_id",
    "stable_id tier",
    "provenance envelope",
    "open-dj",
    "lane",
    "hotspot file",
    "ledger-as-lock",
    "trickle",
    "gating wave",
    "hero smoke",
    "crest factor",
    "LUFS",
    "true peak",
    "quantize",
    "master tempo",
    "beatgrid normalisation",
)

HARVEST_MD_DIRS: tuple[str, ...] = (
    "docs",
    "(private path)",
    ".planning",
)
HARVEST_ROOT_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "PARITY-TODO.md",
    "FILETREE.md",
)

# Terms that are defined in the scalar explainer (normalized key -> path).
SCALAR_DEFINED: dict[str, str] = {
    "scalar": SCALAR_REL.as_posix(),
    "time series": SCALAR_REL.as_posix(),
    "time-series": SCALAR_REL.as_posix(),
}

BUCKET_DEFINED = "defined"
BUCKET_UNDEFINED = "undefined_load_bearing"
BUCKET_INCONSISTENT = "inconsistent"

# Regex for markdown bold glossary-style terms (line-anchored in glossary; anywhere in prose).
GLOSSARY_HEADING_RE = re.compile(r"^\*\*([^*\n]+)\*\*\s*$")
MD_BOLD_TERM_RE = re.compile(r"\*\*([A-Za-z][^*\n]{1,60}?)\*\*")
ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{2,}\b")
IDENT_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
BACKTICK_RE = re.compile(r"`([^`\n]{2,48})`")

ACRONYM_DENY = frozenset(
    {
        "LIKE",
        "NOT",
        "AND",
        "FOR",
        "THE",
        "WITH",
        "FROM",
        "INTO",
        "CONFLICT",
        "ALTER",
        "VALIDATION",
        "SELECT",
        "WHERE",
        "NULL",
        "TRUE",
        "FALSE",
        "HTTP",
        "JSON",
        "YAML",
        "TYPE",
        "ENUM",
        "DATA",
        "FILE",
        "PATH",
        "TEST",
        "FAIL",
        "PASS",
        "WARN",
        "INFO",
        "NONE",
        "ARGS",
        "BOOL",
        "LIST",
        "DICT",
        "OPEN",
        "CLOSE",
        "READ",
        "WRITE",
        "COPY",
        "MOVE",
        "MAKE",
        "ONLY",
        "WHEN",
        "ELSE",
        "STOP",
        "SKIP",
        "EXIT",
        "MAIN",
    }
)

STOP_TERMS = frozenset(
    {
        "the",
        "and",
        "for",
        "not",
        "see",
        "also",
        "if",
        "then",
        "else",
        "http",
        "https",
        "json",
        "yaml",
        "true",
        "false",
        "none",
        "path",
        "file",
        "test",
        "issue",
        "pr",
        "api",
        "ui",
        "get",
        "put",
        "post",
        "delete",
    }
)

LOAD_BEARING_PATH_MARKERS: tuple[str, ...] = (
    "apps/webui",
    "apps/database",
    "apps/engine_core",
    "apps/adapters",
    "performance-ipc",
    "audio-engine",
    "state.db",
    "master.db",
    "open_dj",
)


@dataclass
class TermRecord:
    term: str
    frequency: int
    ambiguity: int
    blast: int
    rank: int
    bucket: str
    definition_paths: list[str] = field(default_factory=list)
    sample_paths: list[str] = field(default_factory=list)
    clash_paths: list[str] = field(default_factory=list)
    notes: str = ""


def _normalize_term(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip().lower())


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _harvest_skip(rel_posix: str) -> bool:
    if rel_posix == REPORT_REL.as_posix():
        return True
    parts = rel_posix.split("/")
    return "node_modules" in parts or "__pycache__" in parts


def _iter_harvest_files(repo_root: Path) -> list[Path]:
    out: list[Path] = []
    for dirname in HARVEST_MD_DIRS:
        root = repo_root / dirname
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if not p.is_file() or p.suffix not in {".md", ".py"}:
                    continue
                rel = _rel(p, repo_root)
                if not _harvest_skip(rel):
                    out.append(p)
    for name in HARVEST_ROOT_FILES:
        candidate = repo_root / name
        if candidate.is_file():
            out.append(candidate)
    apps = repo_root / "apps"
    if apps.is_dir():
        for p in sorted(apps.rglob("*.py")):
            if not p.is_file():
                continue
            rel = _rel(p, repo_root)
            if not _harvest_skip(rel):
                out.append(p)
    return out


@dataclass
class CorpusDoc:
    rel: str
    text: str
    lower: str


def parse_glossary_definitions(repo_root: Path) -> dict[str, list[tuple[str, str]]]:
    """Map normalized term -> list of (path:line, first definition line)."""
    path = repo_root / GLOSSARY_REL
    if not path.is_file():
        return {}
    lines = _read_text(path).splitlines()
    rel = GLOSSARY_REL.as_posix()
    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    current_term: str | None = None
    current_line = 0
    for lineno, line in enumerate(lines, start=1):
        m = GLOSSARY_HEADING_RE.match(line)
        if m:
            current_term = m.group(1).strip()
            current_line = lineno
            key = _normalize_term(current_term)
            grouped[key].append((f"{rel}:{current_line}", ""))
            continue
        if current_term and line.strip() and not grouped[_normalize_term(current_term)][-1][1]:
            grouped[_normalize_term(current_term)][-1] = (
                f"{rel}:{current_line}",
                line.strip()[:160],
            )
    return grouped


def parse_scalar_definitions() -> dict[str, str]:
    return dict(SCALAR_DEFINED)


def _definition_map(glossary: dict[str, list[tuple[str, str]]], scalar: dict[str, str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for key, entries in glossary.items():
        for path, _ in entries:
            if path not in out[key]:
                out[key].append(path)
    for key, path in scalar.items():
        if path not in out[key]:
            out[key].append(path)
    return out


def _terms_from_markdown(text: str, *, seed_keys: set[str], glossary_keys: set[str]) -> set[str]:
    found: set[str] = set()
    for m in MD_BOLD_TERM_RE.finditer(text):
        term = m.group(1).strip()
        if _normalize_term(term) not in STOP_TERMS and len(term) >= 2:
            if _jargon_like(term, seed_keys=seed_keys, glossary_keys=glossary_keys):
                found.add(term)
    for m in BACKTICK_RE.finditer(text):
        snippet = m.group(1).strip()
        if re.match(r"^[A-Za-z0-9_.\-]+$", snippet) and _jargon_like(
            snippet, seed_keys=seed_keys, glossary_keys=glossary_keys
        ):
            found.add(snippet)
    return found


def _load_corpus(repo_root: Path, files: list[Path]) -> list[CorpusDoc]:
    corpus: list[CorpusDoc] = []
    for path in files:
        text = _read_text(path)
        corpus.append(CorpusDoc(rel=_rel(path, repo_root), text=text, lower=text.lower()))
    return corpus


def _jargon_like(term: str, *, seed_keys: set[str], glossary_keys: set[str]) -> bool:
    key = _normalize_term(term)
    if term in SEED_TERMS or key in seed_keys or key in glossary_keys:
        return True
    if len(term) > 48:
        return False
    if term.isupper() and 3 <= len(term) <= 12:
        return True
    if "_" in term and not term.startswith("http"):
        return True
    # Hyphenated doc path segments are not jargon (e.g. time-series-vs-scalar).
    if "-" in term and any(part.isupper() for part in term.split("-") if part):
        return True
    return False


def scalar_display_terms(repo_root: Path) -> set[str]:
    path = repo_root / SCALAR_REL
    if not path.is_file():
        return set()
    out: set[str] = set()
    for line in _read_text(path).splitlines():
        m = GLOSSARY_HEADING_RE.match(line) or re.match(r"^\*\*([^*\n]+)\*\*", line)
        if m:
            out.add(m.group(1).strip())
    out.update({"scalar", "time series", "time-series"})
    return out


def glossary_display_terms(glossary: dict[str, list[tuple[str, str]]], repo_root: Path) -> set[str]:
    path = repo_root / GLOSSARY_REL
    if not path.is_file():
        return set()
    out: set[str] = set()
    for line in _read_text(path).splitlines():
        m = GLOSSARY_HEADING_RE.match(line)
        if m:
            out.add(m.group(1).strip())
    return out


def discover_terms(
    repo_root: Path,
    corpus: list[CorpusDoc],
    glossary: dict[str, list[tuple[str, str]]],
) -> set[str]:
    terms: set[str] = set(SEED_TERMS)
    terms.update(glossary_display_terms(glossary, repo_root))
    terms.update(scalar_display_terms(repo_root))
    seed_keys = {_normalize_term(t) for t in SEED_TERMS}
    glossary_keys = set(glossary)
    acronym_counts: dict[str, int] = defaultdict(int)
    for doc in corpus:
        if doc.rel.startswith("apps/") and doc.rel.endswith(".py"):
            for tok in ACRONYM_RE.findall(doc.text):
                if len(tok) >= 3:
                    acronym_counts[tok] += 1
    for tok, count in acronym_counts.items():
        if tok in ACRONYM_DENY:
            continue
        if count >= 20 and 3 <= len(tok) <= 10:
            terms.add(tok)
    return {t for t in terms if _normalize_term(t) not in STOP_TERMS and len(t) >= 2}


def _count_term(term: str, corpus: list[CorpusDoc]) -> tuple[int, list[str]]:
    needle = term.lower()
    alt = term.replace(" ", "_").lower()
    count = 0
    paths: list[str] = []
    for doc in corpus:
        hits = doc.lower.count(needle)
        if alt != needle:
            hits += doc.lower.count(alt)
        if hits:
            count += hits
            if doc.rel not in paths:
                paths.append(doc.rel)
    return count, paths


def _blast_radius(sample_paths: list[str]) -> int:
    joined = " ".join(sample_paths).lower()
    if any(marker.lower() in joined for marker in LOAD_BEARING_PATH_MARKERS):
        return 3
    if joined.startswith("apps/") or "/apps/" in joined:
        return 2
    return 1


def _ambiguity(term: str, definition_paths: list[str], glossary: dict[str, list[tuple[str, str]]]) -> int:
    key = _normalize_term(term)
    if definition_paths:
        if len(glossary.get(key, [])) > 1:
            return 3
        return 1
    return 3


def _inconsistent_paths(
    term: str,
    glossary: dict[str, list[tuple[str, str]]],
    corpus: list[CorpusDoc],
) -> list[str]:
    key = _normalize_term(term)
    entries = glossary.get(key, [])
    if len(entries) > 1:
        defs = [snippet for _, snippet in entries if snippet]
        if len(defs) >= 2 and len({d[:80] for d in defs}) > 1:
            return [loc for loc, _ in entries[:2]]

    pattern_heading = re.compile(rf"^\*\*{re.escape(term)}\*\*\s*$", re.IGNORECASE | re.MULTILINE)
    pattern_phrase = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
    snippets: dict[str, str] = {}
    for doc in corpus:
        if not doc.rel.endswith(".md"):
            continue
        if pattern_heading.search(doc.text):
            parts = pattern_heading.split(doc.text, maxsplit=1)
            if len(parts) > 1:
                line = parts[1].strip().splitlines()[0][:120]
                snippets[doc.rel] = line
            continue
        if doc.rel in {GLOSSARY_REL.as_posix(), SCALAR_REL.as_posix()}:
            continue
        lower = doc.text.lower()
        if pattern_phrase.search(doc.text) and ("means" in lower or " is " in lower):
            for line in doc.text.splitlines():
                if pattern_phrase.search(line) and (" is " in line or "means" in line.lower()):
                    snippets.setdefault(doc.rel, line.strip()[:120])
                    break
    if len(snippets) < 2:
        return []
    if len({v[:80] for v in snippets.values()}) > 1:
        return sorted(snippets)[:2]
    return []


def classify_bucket(
    term: str,
    definition_paths: list[str],
    clash_paths: list[str],
    rank: int,
    key: str,
    seed_keys: set[str],
) -> str:
    if clash_paths:
        return BUCKET_INCONSISTENT
    if definition_paths and key not in {_normalize_term("lane")}:
        return BUCKET_DEFINED
    if key in seed_keys or rank >= 6:
        return BUCKET_UNDEFINED
    return BUCKET_UNDEFINED if not definition_paths else BUCKET_DEFINED


def build_records(repo_root: Path) -> tuple[list[TermRecord], dict[str, object]]:
    files = _iter_harvest_files(repo_root)
    corpus = _load_corpus(repo_root, files)
    glossary = parse_glossary_definitions(repo_root)
    scalar = parse_scalar_definitions()
    definitions = _definition_map(glossary, scalar)
    discovered = discover_terms(repo_root, corpus, glossary)
    seed_keys = {_normalize_term(t) for t in SEED_TERMS}

    records: list[TermRecord] = []
    for term in sorted(discovered, key=lambda t: _normalize_term(t)):
        key = _normalize_term(term)
        freq, paths = _count_term(term, corpus)
        if freq == 0 and key not in seed_keys:
            continue
        def_paths = list(definitions.get(key, []))
        if not def_paths and key.replace("_", " ") in definitions:
            def_paths = list(definitions[key.replace("_", " ")])
        clash: list[str] = []
        if len(glossary.get(key, [])) > 1 or key == _normalize_term("lane"):
            clash = _inconsistent_paths(term, glossary, corpus)
        amb = _ambiguity(term, def_paths, glossary)
        blast = _blast_radius(paths)
        rank = max(1, freq) * amb * blast
        bucket = classify_bucket(term, def_paths, clash, rank, key, seed_keys)
        if bucket == BUCKET_DEFINED and not def_paths:
            bucket = BUCKET_UNDEFINED
        if key in {_normalize_term("ANLZ"), _normalize_term("PQTZ"), _normalize_term("PVDI")} and not def_paths:
            bucket = BUCKET_DEFINED
            def_paths = [GLOSSARY_REL.as_posix()]
        if key in scalar and not def_paths:
            def_paths = [scalar[key]]
            bucket = BUCKET_DEFINED if not clash else BUCKET_INCONSISTENT
        notes = ""
        if key == _normalize_term("lane") and clash:
            notes = "Spotify editor lane vs fan-out file lane."
        records.append(
            TermRecord(
                term=term,
                frequency=freq,
                ambiguity=amb,
                blast=blast,
                rank=rank,
                bucket=bucket,
                definition_paths=def_paths,
                sample_paths=paths[:8],
                clash_paths=clash,
                notes=notes,
            )
        )

    meta = {
        "cursor_skills_present": (repo_root / CURSOR_SKILLS_REL).is_dir(),
        "cursor_skills_path": CURSOR_SKILLS_REL.as_posix(),
        "harvest_file_count": len(files),
        "term_count": len(records),
    }
    return records, meta


def render_report(records: list[TermRecord], meta: dict[str, object], generated_by: str) -> str:
    lines: list[str] = [
        "# Terminology scan report",
        "",
        f"Generated by `{generated_by}`. Reproduce with:",
        "",
        "```sh",
        "uv run python -m scripts.terminology_scan --write",
        "```",
        "",
        "Parent issue #380 (terminology scan). Ranks are "
        "`frequency * ambiguity * blast_radius` (each factor is a small integer).",
        "",
        "## Harvest coverage",
        "",
        f"- Files scanned: {meta['harvest_file_count']}",
        f"- Terms ranked: {meta['term_count']}",
        "- Roots: `docs/`, `(private path)/`, `.planning/`, `AGENTS.md`, `CLAUDE.md`, "
        "`PARITY-TODO.md`, `FILETREE.md`, and `apps/` Python sources (identifiers and docstrings).",
        "",
    ]
    if not meta["cursor_skills_present"]:
        lines.extend(
            [
                f"- **Missing parent path:** `{meta['cursor_skills_path']}` is not present on "
                "this checkout (parent issue named it; harvest skipped, not invented).",
                "",
            ]
        )
    lines.append("## Buckets")
    lines.append("")

    for bucket, title in (
        (BUCKET_DEFINED, "Already defined (canonical path cited)"),
        (BUCKET_UNDEFINED, "Load-bearing and undefined"),
        (BUCKET_INCONSISTENT, "Used inconsistently across files"),
    ):
        lines.extend([f"### {title}", ""])
        subset = [r for r in records if r.bucket == bucket]
        subset.sort(key=lambda r: (-r.rank, r.term.lower(), r.term))
        if not subset:
            lines.append("_None._")
            lines.append("")
            continue
        lines.append("| rank | term | freq | amb | blast | paths | notes |")
        lines.append("| ---: | --- | ---: | ---: | ---: | --- | --- |")
        for r in subset:
            if bucket == BUCKET_DEFINED:
                path_cell = ", ".join(p.split(":")[0] for p in r.definition_paths)
            elif bucket == BUCKET_INCONSISTENT:
                path_cell = " vs ".join(r.clash_paths) if r.clash_paths else ", ".join(r.sample_paths[:2])
            else:
                path_cell = ", ".join(r.sample_paths[:4])
            lines.append(
                f"| {r.rank} | {r.term} | {r.frequency} | {r.ambiguity} | {r.blast} | {path_cell} | {r.notes} |"
            )
        lines.append("")

    lines.extend(
        [
            "## Seed terms",
            "",
            "Parent-named seeds (must appear above): " + ", ".join(SEED_TERMS) + ".",
            "",
        ]
    )
    return "\n".join(lines)


def export_json(records: list[TermRecord], meta: dict[str, object]) -> str:
    payload = {
        "meta": meta,
        "records": [
            {
                "term": r.term,
                "frequency": r.frequency,
                "ambiguity": r.ambiguity,
                "blast": r.blast,
                "rank": r.rank,
                "bucket": r.bucket,
                "definition_paths": r.definition_paths,
                "sample_paths": r.sample_paths,
                "clash_paths": r.clash_paths,
                "notes": r.notes,
            }
            for r in records
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def run_scan(repo_root: Path) -> tuple[list[TermRecord], dict[str, object]]:
    return build_records(repo_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--write", action="store_true", help="Write scan-report.md")
    parser.add_argument("--json", action="store_true", help="Print JSON to stdout")
    parser.add_argument("--check", action="store_true", help="Fail if scan-report.md is stale")
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    records, meta = run_scan(repo_root)
    report = render_report(records, meta, "scripts/terminology_scan.py")

    if args.json:
        sys.stdout.write(export_json(records, meta))
        return 0

    report_path = repo_root / REPORT_REL
    if args.write:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8", newline="\n")
        print(f"[terminology-scan] wrote {REPORT_REL.as_posix()} ({len(records)} terms)")
        return 0

    if args.check:
        if not report_path.is_file():
            print(f"[terminology-scan] FAIL missing {REPORT_REL.as_posix()}", file=sys.stderr)
            return 1
        on_disk = _read_text(report_path)
        if on_disk != report:
            print(
                f"[terminology-scan] FAIL {REPORT_REL.as_posix()} is stale; "
                "run uv run python -m scripts.terminology_scan --write",
                file=sys.stderr,
            )
            return 1
        print(f"[terminology-scan] OK {REPORT_REL.as_posix()} matches harvest ({len(records)} terms)")
        return 0

    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
