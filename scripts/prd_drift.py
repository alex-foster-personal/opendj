"""Advisory drift check for the feature PRD pages (`(private path)/`, schema `opendj-prd/1`).

Mini-PRD
- Reads `reqs.json` (built first through `scripts.build_reqs_json` when it is a gitignored artifact) and every `(private path)/<slug>/prd.md` and reports where they disagree.
- ADVISORY: exits 0 and prints the report unless `--strict` is given. It is not a merge gate:
  nothing in CI, the lander or a PR template may require it, and a feature PR never has to
  touch a page (the next site build picks up its requirement ids through the page's `areas:`).
- Standard library plus PyYAML (already a repo dependency); adds no dependency.

Checks (plan section 4.3)
- D1  each `reqs.json` area is on exactly one page, and each page area exists in `reqs.json`.
- D2  front matter matches `(private path)`: required fields, enums, `why` length,
      slug equals the directory name, `group` exists in `_groups.yaml`, top pages have no group.
- D3  requirement ids cited in a page body exist in `reqs.json`; alias targets exist.
- D4  paths in `adrs`, `specs`, `ui_contracts` exist; each `stories` id matches a `*.stories.ts`
      title plus export (id form `Title/Path--kebab-export`; the title is compared verbatim).
      The title is the CSF meta (default export) title: a literal, or a `const X = '...'` in the
      same file. A title not statically resolvable (a template with `${}`) makes the id an
      `UNRESOLVED` warning, never an error.
- D5  each parity entry has `http`, `cli` and `mcp`, or a `gap`.
- D6  a top page in `review` or `accepted` has a non-empty `quality_signal` (a warning while draft).

Usage
    python3 scripts/prd_drift.py [--repo-root DIR] [--json] [--strict]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

SCHEMA = "opendj-prd/1"
FEATURES_DIR = Path("(private path)")
GROUPS_FILE = "_groups.yaml"
REQS_FILE = Path("reqs.json")
REQ_FRAGMENTS_DIR = Path("(private path)")
REQS_RELEASE_BUCKETS = ("v1", "v1.1", "v2", "v3")
SLUG_PATTERN = re.compile(r"^[a-z0-9-]+$")
STATUSES = ("draft", "review", "accepted")
TIERS = ("top", "nested")
WHY_MAX_CHARS = 120
LIST_FIELDS = ("adrs", "specs", "ui_contracts", "stories", "areas")
REQUIRED_FIELDS = (
    "schema", "slug", "title", "tier", "why", "areas", "status", "owner",
    "adrs", "specs", "ui_contracts", "stories", "parity", "quality_signal", "aliases",
)  # fmt: skip
QUALITY_KEYS = ("proxy", "calibration", "vetoes")
PARITY_TWINS = ("http", "cli", "mcp")
DRAFT_SECTION = re.compile(r"^#{1,6}\s+Draft requirements\b", re.MULTILINE | re.IGNORECASE)
REQ_ID_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*)-(?:\d+[a-z]?|S\d+)\b")
STORY_DEFAULT_EXPORT = re.compile(r"^export\s+default\s+(\{|\w+)", re.MULTILINE)
STORY_QUOTES = "'\"`"
STORY_EXPORT = re.compile(r"^export\s+const\s+(\w+)", re.MULTILINE)
STORIES_SKIP_DIRS = ("node_modules", ".git", "dist", "build")


# ----- Data ---------------------------------------------------------------


@dataclass
class Finding:
    """One drift finding: which check, how severe, which page, what is wrong."""

    check: str
    severity: str  # "error" or "warning"
    page: str
    message: str


@dataclass
class Page:
    """A parsed `prd.md`: its directory name, front matter (empty when unreadable) and body."""

    directory: str
    meta: dict = field(default_factory=dict)
    body: str = ""
    parse_error: str = ""


@dataclass
class Report:
    """Everything one run found, plus the counts the summary line prints."""

    findings: list[Finding]
    page_count: int
    area_count: int

    def errors(self) -> list[Finding]:
        """The findings that `--strict` turns into a non-zero exit."""
        return [f for f in self.findings if f.severity == "error"]

    def warnings(self) -> list[Finding]:
        """The findings that never affect the exit code."""
        return [f for f in self.findings if f.severity == "warning"]


# ----- Loading ------------------------------------------------------------


def ensure_reqs_json(repo_root: Path) -> None:
    """Build `reqs.json` when the tree keeps requirements as fragments (it is a gitignored build artifact).

    Runs `python -m scripts.build_reqs_json` in the checkout, the module `ensure_output()` lives in; a failed
    build raises. A tree without `(private path)/` keeps a committed `reqs.json`, read as is.
    """
    if (repo_root / REQ_FRAGMENTS_DIR).is_dir():
        subprocess.run([sys.executable, "-m", "scripts.build_reqs_json"], cwd=repo_root, check=True)


def load_reqs(repo_root: Path) -> tuple[set[str], set[str]]:
    """Return (area prefixes, requirement ids) read from `reqs.json`, built first when it is an artifact."""
    ensure_reqs_json(repo_root)
    data = json.loads((repo_root / REQS_FILE).read_text(encoding="utf-8"))
    areas: set[str] = set()
    ids: set[str] = set()
    for bucket in REQS_RELEASE_BUCKETS:
        for area, category in (data.get(bucket) or {}).items():
            areas.add(area)
            ids.update(req["id"] for req in category.get("requirements", []))
    return areas, ids


def load_group_slugs(repo_root: Path) -> set[str]:
    """Return the group slugs declared in `(private path)`."""
    path = repo_root / FEATURES_DIR / GROUPS_FILE
    if not path.is_file():
        return set()
    groups = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("groups") or []
    return {g["slug"] for g in groups if isinstance(g, dict) and "slug" in g}


def split_front_matter(text: str) -> tuple[str, str]:
    """Split a page into (front matter text, body); raise ValueError when it has no front matter."""
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", text, re.DOTALL)
    if match is None:
        raise ValueError("no `---` delimited front matter at the top of the file")
    return match.group(1), match.group(2)


def load_pages(repo_root: Path) -> list[Page]:
    """Parse every `(private path)/<slug>/prd.md`, keeping parse failures on the page."""
    pages: list[Page] = []
    for path in sorted((repo_root / FEATURES_DIR).glob("*/prd.md")):
        page = Page(directory=path.parent.name)
        try:
            front_text, page.body = split_front_matter(path.read_text(encoding="utf-8"))
            meta = yaml.safe_load(front_text)
            if not isinstance(meta, dict):
                raise ValueError("front matter is not a mapping")
            page.meta = meta
        except (ValueError, yaml.YAMLError) as exc:
            page.parse_error = str(exc).splitlines()[0]
        pages.append(page)
    return pages


# ----- Checks -------------------------------------------------------------


def _error(check: str, page: str, message: str) -> Finding:
    """Build an error finding."""
    return Finding(check, "error", page, message)


def _page_areas(page: Page) -> list[str]:
    """The page's `areas:` entries that are strings (bad shapes are reported by D2)."""
    areas = page.meta.get("areas")
    return [a for a in areas if isinstance(a, str)] if isinstance(areas, list) else []


def check_d1_areas(pages: list[Page], reqs_areas: set[str]) -> list[Finding]:
    """D1: every reqs.json area sits on exactly one page and every page area exists."""
    findings: list[Finding] = []
    homes: dict[str, list[str]] = defaultdict(list)
    for page in pages:
        for area in _page_areas(page):
            homes[area].append(page.directory)
            if area not in reqs_areas:
                findings.append(_error("D1", page.directory, f"area {area} is not in reqs.json"))
    for area in sorted(reqs_areas):
        if not homes[area]:
            findings.append(_error("D1", "-", f"area {area} is on no page"))
    for area, owners in sorted(homes.items()):
        if len(owners) > 1:
            findings.append(_error("D1", "-", f"area {area} is on {len(owners)} pages: {', '.join(owners)}"))
    return findings


def _is_text_list(value: object) -> bool:
    """True for a list whose items are all non-empty strings."""
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def check_d2_schema(pages: list[Page], group_slugs: set[str]) -> list[Finding]:
    """D2: front matter matches the `opendj-prd/1` contract in `(private path)`."""
    findings: list[Finding] = []
    for page in pages:
        where, meta = page.directory, page.meta
        if page.parse_error:
            findings.append(_error("D2", where, f"front matter unreadable: {page.parse_error}"))
            continue
        for name in REQUIRED_FIELDS:
            if name not in meta:
                findings.append(_error("D2", where, f"missing required field `{name}`"))
        if meta.get("schema", SCHEMA) != SCHEMA:
            findings.append(_error("D2", where, f"schema must be `{SCHEMA}`, got {meta['schema']!r}"))
        slug = meta.get("slug")
        if slug is not None and (not isinstance(slug, str) or not SLUG_PATTERN.match(slug)):
            findings.append(_error("D2", where, f"slug {slug!r} must match [a-z0-9-]+"))
        elif slug is not None and slug != page.directory:
            findings.append(_error("D2", where, f"slug {slug!r} must equal the directory name"))
        for name in ("title", "owner"):
            if name in meta and not (isinstance(meta[name], str) and meta[name].strip()):
                findings.append(_error("D2", where, f"`{name}` must be a non-empty string"))
        findings += _tier_and_group_findings(where, meta, group_slugs)
        why = meta.get("why")
        if "why" in meta and not (isinstance(why, str) and 1 <= len(why) <= WHY_MAX_CHARS):
            findings.append(_error("D2", where, f"`why` must be 1 to {WHY_MAX_CHARS} characters"))
        if "status" in meta and meta["status"] not in STATUSES:
            findings.append(_error("D2", where, f"status {meta['status']!r} must be one of {STATUSES}"))
        for name in LIST_FIELDS:
            if name in meta and not _is_text_list(meta[name]):
                findings.append(_error("D2", where, f"`{name}` must be a list of non-empty strings"))
        if "parity" in meta and not (
            isinstance(meta["parity"], list) and all(isinstance(p, dict) for p in meta["parity"])
        ):
            findings.append(_error("D2", where, "`parity` must be a list of mappings"))
        for name in ("quality_signal", "aliases"):
            if name in meta and not isinstance(meta[name], dict):
                findings.append(_error("D2", where, f"`{name}` must be a mapping"))
    return findings


def _tier_and_group_findings(where: str, meta: dict, group_slugs: set[str]) -> list[Finding]:
    """D2 rules for `tier` and `group`: top pages have no group, nested pages name a real one."""
    tier = meta.get("tier")
    if "tier" not in meta:
        return []
    if tier not in TIERS:
        return [_error("D2", where, f"tier {tier!r} must be one of {TIERS}")]
    group = meta.get("group")
    if group is not None and not isinstance(group, str):
        return [_error("D2", where, f"`group` must be a string, got {group!r}")]
    if tier == "top" and group is not None:
        return [_error("D2", where, "a top page must not have a `group`")]
    if tier == "nested" and group is None:
        return [_error("D2", where, "a nested page needs a `group`")]
    if tier == "nested" and group not in group_slugs:
        return [_error("D2", where, f"group {group!r} is not in {GROUPS_FILE}")]
    return []


def id_prefixes(req_ids: set[str]) -> set[str]:
    """The id prefixes in use (an id's prefix can differ from its area key: LIBTAG-* is filed under CHROME)."""
    return {m.group(1) for rid in req_ids if (m := REQ_ID_PATTERN.fullmatch(rid))}


def cited_requirement_ids(body: str, prefixes: set[str]) -> set[str]:
    """Requirement-looking ids in the body whose prefix is in use, outside the Draft requirements section."""
    cut = DRAFT_SECTION.search(body)
    written = body[: cut.start()] if cut else body
    return {m.group(0) for m in REQ_ID_PATTERN.finditer(written) if m.group(1) in prefixes}


def check_d3_requirement_ids(pages: list[Page], req_ids: set[str]) -> list[Finding]:
    """D3: ids cited in a page body and alias targets exist in reqs.json."""
    findings: list[Finding] = []
    prefixes = id_prefixes(req_ids)
    for page in pages:
        for rid in sorted(cited_requirement_ids(page.body, prefixes) - req_ids):
            findings.append(_error("D3", page.directory, f"cites {rid}, which is not in reqs.json"))
        aliases = page.meta.get("aliases")
        for local_id, target in sorted((aliases if isinstance(aliases, dict) else {}).items()):
            if target not in req_ids:
                findings.append(
                    _error("D3", page.directory, f"alias {local_id} points at {target}, which is not in reqs.json")
                )
    return findings


def _kebab(export_name: str) -> str:
    """Storybook-style kebab-case of an export name: `ShowOnHover` and `showOnHover` both give `show-on-hover`."""
    spaced = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])|_+",
        "-",
        export_name,
    )
    return re.sub(r"-+", "-", spaced).strip("-").lower()


def _skip_literal_or_comment(text: str, index: int) -> int:
    """If a string, template or comment starts at `index`, the index just past it; otherwise `index`."""
    char = text[index]
    if char in STORY_QUOTES:
        index += 1
        while index < len(text) and text[index] != char:
            index += 2 if text[index] == "\\" else 1
        return index + 1
    elif text.startswith("//", index):
        newline = text.find("\n", index)
        return len(text) if newline == -1 else newline
    elif text.startswith("/*", index):
        close = text.find("*/", index + 2)
        return len(text) if close == -1 else close + 2
    return index


def _object_body_at(text: str, open_brace: int) -> str | None:
    """The text between the `{` at `open_brace` and its matching `}`, or None when unbalanced."""
    depth = 0
    index = open_brace
    while index < len(text):
        skipped = _skip_literal_or_comment(text, index)
        if skipped != index:
            index = skipped
            continue
        if text[index] in "{[(":
            depth += 1
        elif text[index] in "}])":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : index]
        index += 1
    return None


def _csf_meta_body(text: str) -> str | None:
    """Body of the object exported as default: `export default {...}` or `export default meta` plus its `const`."""
    match = STORY_DEFAULT_EXPORT.search(text)
    if match is None:
        return None
    elif match.group(1) == "{":
        return _object_body_at(text, match.end() - 1)
    declaration = re.search(rf"\b(?:const|let|var)\s+{match.group(1)}\b[^=]*=\s*\{{", text)
    return None if declaration is None else _object_body_at(text, declaration.end() - 1)


STORY_TITLE_KEY = re.compile(r"title\s*:\s*")


def _top_level_title_value(body: str) -> str | None:
    """Raw source of the meta object's own `title:` value (a nested `title:` key is ignored)."""
    depth = 0
    index = 0
    value_start: int | None = None
    while index < len(body):
        skipped = _skip_literal_or_comment(body, index)
        if skipped != index:
            index = skipped
            continue
        char = body[index]
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif value_start is not None and depth == 0 and char == ",":
            return body[value_start:index].strip()
        elif value_start is None and depth == 0 and (key := STORY_TITLE_KEY.match(body, index)) is not None:
            if index == 0 or not (body[index - 1].isalnum() or body[index - 1] in "_$."):
                value_start = key.end()
                index = key.end()
                continue
        index += 1
    return None if value_start is None else body[value_start:].strip()


def _resolve_title_expression(expression: str, text: str) -> str | None:
    """A string literal (or plain template), or a `const X = '...'` reference in the same file; else None."""
    literal = re.fullmatch(r"(['\"`])((?:\\.|(?!\1).)*)\1", expression, re.DOTALL)
    if literal is not None:
        return None if literal.group(1) == "`" and "${" in literal.group(2) else literal.group(2)
    elif re.fullmatch(r"[A-Za-z_$][\w$]*", expression):
        declaration = re.search(
            rf"^(?:export\s+)?const\s+{re.escape(expression)}(?:\s*:\s*string)?\s*=\s*(['\"])((?:\\.|(?!\1).)*)\1\s*(?:;|$)",
            text,
            re.MULTILINE,
        )
        return None if declaration is None else declaration.group(2)
    return None


def csf_title(text: str) -> str | None:
    """The Storybook CSF meta title of a stories file, or None when it cannot be resolved statically."""
    body = _csf_meta_body(text)
    expression = None if body is None else _top_level_title_value(body)
    return None if expression is None else _resolve_title_expression(expression, text)


def declared_stories(repo_root: Path) -> tuple[set[str], dict[str, set[str]]]:
    """Every `<title>--<kebab-export>` id declared by a `*.stories.ts`, plus the unresolved files.

    The second value maps each file whose title cannot be resolved statically to its kebab export names.
    """
    ids: set[str] = set()
    unresolved: dict[str, set[str]] = {}
    for path in repo_root.rglob("*.stories.ts"):
        relative = path.relative_to(repo_root)
        if any(part in STORIES_SKIP_DIRS for part in relative.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        exports = {_kebab(e) for e in STORY_EXPORT.findall(text) if e != "default"}
        title = csf_title(text)
        if title is None:
            unresolved[relative.as_posix()] = exports
        else:
            ids.update(f"{title}--{e}" for e in exports)
    return ids, unresolved


def check_d4_references(pages: list[Page], repo_root: Path) -> list[Finding]:
    """D4: cited ADR, spec and UI-contract paths exist and story ids match a stories file."""
    findings: list[Finding] = []
    stories: tuple[set[str], dict[str, set[str]]] | None = None
    for page in pages:
        for name in ("adrs", "specs", "ui_contracts"):
            for rel in page.meta.get(name) if _is_text_list(page.meta.get(name)) else []:
                if not (repo_root / rel).exists():
                    findings.append(_error("D4", page.directory, f"{name} path {rel} does not exist"))
        story_ids = page.meta.get("stories") if _is_text_list(page.meta.get("stories")) else []
        if story_ids and stories is None:
            stories = declared_stories(repo_root)
        for story_id in story_ids:
            declared, unresolved = stories or (set(), {})
            export_name = story_id.rpartition("--")[2]
            unresolved_files = sorted(f for f, exports in unresolved.items() if export_name in exports)
            if story_id in declared:
                continue
            elif unresolved_files:
                message = f"story {story_id} UNRESOLVED: title unresolved in {', '.join(unresolved_files)}"
                findings.append(Finding("D4", "warning", page.directory, message))
            else:
                findings.append(
                    _error("D4", page.directory, f"story {story_id} matches no *.stories.ts title plus export")
                )
    return findings


def check_d5_parity(pages: list[Page]) -> list[Finding]:
    """D5: each parity entry has http, cli and mcp twins or a named gap."""
    findings: list[Finding] = []
    for page in pages:
        entries = page.meta.get("parity")
        for index, entry in enumerate(entries if isinstance(entries, list) else []):
            if not isinstance(entry, dict):
                continue
            action = entry.get("action") or f"entry {index + 1}"
            if not entry.get("action"):
                findings.append(_error("D5", page.directory, f"parity entry {index + 1} has no `action`"))
            missing = [twin for twin in PARITY_TWINS if not entry.get(twin)]
            if missing and not entry.get("gap"):
                findings.append(
                    _error("D5", page.directory, f"parity `{action}` has no {', '.join(missing)} twin and no `gap`")
                )
    return findings


def _quality_signal_is_filled(signal: object) -> bool:
    """True when the signal names a `mimicked` reference or fills proxy, calibration and vetoes."""
    if not isinstance(signal, dict):
        return False
    return bool(signal.get("mimicked")) or all(signal.get(key) for key in QUALITY_KEYS)


def check_d6_quality_signal(pages: list[Page]) -> list[Finding]:
    """D6: a top page in review or accepted has a quality signal; a draft top page only warns."""
    findings: list[Finding] = []
    for page in pages:
        meta = page.meta
        if meta.get("tier") != "top" or _quality_signal_is_filled(meta.get("quality_signal")):
            continue
        if meta.get("status") in ("review", "accepted"):
            findings.append(_error("D6", page.directory, f"top page in {meta['status']} has an empty quality_signal"))
        elif meta.get("status") == "draft":
            findings.append(Finding("D6", "warning", page.directory, "top page has no quality_signal yet (draft)"))
    return findings


# ----- Run and report -----------------------------------------------------


def run_checks(repo_root: Path) -> Report:
    """Run D1 to D6 against a repo checkout and return the report."""
    if not (repo_root / FEATURES_DIR).is_dir():
        return Report([_error("D1", "-", f"{FEATURES_DIR} does not exist")], 0, 0)
    reqs_areas, req_ids = load_reqs(repo_root)
    pages = load_pages(repo_root)
    findings = [
        *check_d1_areas(pages, reqs_areas),
        *check_d2_schema(pages, load_group_slugs(repo_root)),
        *check_d3_requirement_ids(pages, req_ids),
        *check_d4_references(pages, repo_root),
        *check_d5_parity(pages),
        *check_d6_quality_signal(pages),
    ]
    return Report(findings, len(pages), len(reqs_areas))


def format_text(report: Report) -> str:
    """Render the report as plain text: one line per finding, then the summary line."""
    lines = [f"{f.severity.upper():7} {f.check} {f.page}: {f.message}" for f in report.findings]
    lines.append(
        f"prd_drift: {report.page_count} pages, {report.area_count} areas, "
        f"{len(report.errors())} errors, {len(report.warnings())} warnings"
    )
    return "\n".join(lines)


def format_json(report: Report) -> str:
    """Render the report as JSON for tools."""
    return json.dumps(
        {
            "pages": report.page_count,
            "areas": report.area_count,
            "errors": [asdict(f) for f in report.errors()],
            "warnings": [asdict(f) for f in report.warnings()],
        },
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry: print the report; exit 1 only with `--strict` and at least one error."""
    parser = argparse.ArgumentParser(description="Advisory drift check for (private path) PRD pages.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--strict", action="store_true", help="exit non-zero when there are errors")
    args = parser.parse_args(argv)
    report = run_checks(args.repo_root)
    print(format_json(report) if args.json else format_text(report))
    return 1 if args.strict and report.errors() else 0


if __name__ == "__main__":
    sys.exit(main())
