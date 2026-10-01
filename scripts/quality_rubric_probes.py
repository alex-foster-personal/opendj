"""Mechanical probes for the scored code-quality rubric."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.sparse_worktree import require_materialized, skip_worktree_paths

# ----- markdown link extraction ------------------------------------------------

_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_MD_AUTOLINK_RE = re.compile(r"<([^>]+)>")
_GITHUB_REPO_RE = re.compile(r"https://github\.com/([^/\s)]+)/([^/\s)]+)")
_VERSION_TOKEN_RE = re.compile(r"\bv\d+(?:\.\d+)*\b", re.IGNORECASE)
_CORPUS_DIR_RE = re.compile(r"conformance/corpus-[^\s/`)\]]+")
_CORPUS_CITE_RE = re.compile(
    r"(?:conformance/corpus-[^\s/`)\]]+|tests/fixtures/conformance/?)"
)
_NAMING_AUTHORITY_RE = re.compile(r"naming authority|not a fetchable location", re.IGNORECASE)
_SERATO_FUTURE_RE = re.compile(r"adds Serato", re.IGNORECASE)

_SKIP_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        "build",
        "dist",
        ".svelte-kit",
        ".vite",
        "storybook-static",
        "test-results",
        "playwright-report",
        ".tmp",
        "target",
        "htmlcov",
    }
)


def _surface_file_matches(
    path: Path, *, suffix: str | None, name_match: str | None
) -> bool:
    if suffix is not None and path.suffix != suffix:
        return False
    if name_match is not None and not fnmatch.fnmatch(path.name, name_match):
        return False
    return True


def _git_ls_surface_files(surface_root: Path, repo_root: Path) -> list[Path] | None:
    try:
        surface_rel = surface_root.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                surface_rel,
            ],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    # No is_file() filter here: a skip-worktree entry is not on disk either, and
    # dropping it would read as "absent" (OPS-45); _iter_surface_files decides.
    return [repo_root / os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw]


def _walk_surface_files(surface_root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(surface_root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIR_NAMES]
        root = Path(dirpath)
        for name in filenames:
            files.append(root / name)
    return files


def _iter_surface_files(
    surface_root: Path,
    repo_root: Path,
    *,
    suffix: str | None = None,
    name_match: str | None = None,
) -> list[Path]:
    if not surface_root.is_dir():
        return []
    listed = _git_ls_surface_files(surface_root, repo_root)
    from_git = listed is not None
    if listed is None:
        listed = _walk_surface_files(surface_root)
    wanted = [
        path for path in listed if _surface_file_matches(path, suffix=suffix, name_match=name_match)
    ]
    if from_git:
        require_materialized(
            repo_root,
            [_repo_relative(path, repo_root) for path in wanted],
            purpose=f"rubric probe over {_repo_relative(surface_root, repo_root)}",
        )
    return sorted(path for path in wanted if path.is_file())


def _iter_markdown_files(surface_root: Path, repo_root: Path) -> list[Path]:
    return _iter_surface_files(surface_root, repo_root, suffix=".md")


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _extract_hrefs(text: str) -> list[str]:
    hrefs = [m.group(1).strip() for m in _MD_LINK_RE.finditer(text)]
    for match in _MD_AUTOLINK_RE.finditer(text):
        href = match.group(1).strip()
        if "://" in href or href.startswith("mailto:"):
            hrefs.append(href)
    return hrefs


def _should_skip_href(href: str) -> bool:
    if not href or href.startswith("#"):
        return True
    lowered = href.lower()
    return lowered.startswith(("http://", "https://", "mailto:"))


def _skip_worktree_targets(repo_root: Path) -> set[Path]:
    """Tracked files a sparse worktree keeps off disk (OPS-45). A link to one is not
    dangling: the commit carries the target, and a full checkout would find it."""
    if not (repo_root / ".git").exists():
        return set()
    return {(repo_root / rel).resolve() for rel in skip_worktree_paths(repo_root)}


def _collect_relative_links(
    surface_root: Path, repo_root: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    escapes: list[dict[str, Any]] = []
    dangling: list[dict[str, Any]] = []
    md_files = _iter_markdown_files(surface_root, repo_root)
    if not md_files:
        return escapes, dangling
    skipped = _skip_worktree_targets(repo_root)
    for md_path in md_files:
        text = md_path.read_text(encoding="utf-8", errors="replace")
        for href in _extract_hrefs(text):
            if _should_skip_href(href):
                continue
            target = (md_path.parent / href).resolve()
            try:
                target.relative_to(surface_root.resolve())
                in_surface = True
            except ValueError:
                in_surface = False
            rel_path = _repo_relative(md_path, repo_root)
            if not in_surface:
                escapes.append(
                    {
                        "code": "B1",
                        "class": "relative_link_escapes_surface",
                        "path": rel_path,
                        "href": href,
                        "detail": "resolves outside surface root",
                    }
                )
            elif not target.exists() and target not in skipped:
                dangling.append(
                    {
                        "code": None,
                        "class": "relative_link_dangling",
                        "path": rel_path,
                        "href": href,
                        "detail": "target missing inside surface root",
                    }
                )
    return escapes, dangling


def _surface_markdown_text(surface_root: Path, repo_root: Path) -> str:
    parts: list[str] = []
    for md_path in _iter_markdown_files(surface_root, repo_root):
        parts.append(md_path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


# ----- probes ------------------------------------------------------------------

def xref_integrity(surface_root: Path, repo_root: Path) -> list[dict[str, Any]]:
    if not surface_root.is_dir():
        return [
            {
                "code": None,
                "class": "probe_unscorable",
                "path": str(surface_root),
                "detail": "surface root is not a directory",
            }
        ]
    md_files = _iter_markdown_files(surface_root, repo_root)
    if not md_files:
        return [
            {
                "code": None,
                "class": "probe_unscorable",
                "path": _repo_relative(surface_root, repo_root),
                "detail": "no markdown files to scan",
            }
        ]
    escapes, dangling = _collect_relative_links(surface_root, repo_root)
    findings = escapes + dangling
    if len(findings) > 20:
        return findings + [
            {
                "code": None,
                "class": "probe_unscorable",
                "path": _repo_relative(surface_root, repo_root),
                "detail": "more than twenty link defects",
            }
        ]
    return findings


def citation_resolves(surface_root: Path, repo_root: Path) -> list[dict[str, Any]]:
    if not surface_root.is_dir():
        return []
    findings: list[dict[str, Any]] = []
    gh_available = shutil.which("gh") is not None
    seen: set[tuple[str, str]] = set()
    for md_path in _iter_markdown_files(surface_root, repo_root):
        text = md_path.read_text(encoding="utf-8", errors="replace")
        rel_path = _repo_relative(md_path, repo_root)
        for owner, repo in _GITHUB_REPO_RE.findall(text):
            key = (owner.lower(), repo.lower())
            if key in seen:
                continue
            seen.add(key)
            if owner.lower() == "example" or repo.lower().startswith("example"):
                findings.append(
                    {
                        "code": "B2",
                        "class": "citation_placeholder",
                        "path": rel_path,
                        "href": f"https://github.com/{owner}/{repo}",
                        "detail": "placeholder GitHub owner or repo",
                    }
                )
                continue
            if not gh_available:
                continue
            result = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}", "--silent"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                findings.append(
                    {
                        "code": "B2",
                        "class": "citation_github_missing",
                        "path": rel_path,
                        "href": f"https://github.com/{owner}/{repo}",
                        "detail": "gh api repos lookup failed",
                    }
                )
    return findings


def docs_tree_agreement(surface_root: Path, repo_root: Path) -> list[dict[str, Any]]:
    if not surface_root.is_dir():
        return []
    _, dangling = _collect_relative_links(surface_root, repo_root)
    findings = list(dangling)
    readme = surface_root / "README.md"
    if readme.is_file():
        readme_text = readme.read_text(encoding="utf-8", errors="replace")
        serato_doc = surface_root / "adapters" / "serato.md"
        if _SERATO_FUTURE_RE.search(readme_text) and serato_doc.is_file():
            findings.append(
                {
                    "code": None,
                    "class": "docs_claim_disagrees_with_tree",
                    "path": _repo_relative(readme, repo_root),
                    "detail": "README says v0.3 adds Serato but adapters/serato.md exists",
                }
            )
    return findings


def _latest_version_token(text: str) -> str | None:
    matches = _VERSION_TOKEN_RE.findall(text)
    if not matches:
        return None
    return matches[-1].lower()


def version_drift(surface_root: Path, repo_root: Path) -> list[dict[str, Any]]:
    if not surface_root.is_dir():
        return []
    findings: list[dict[str, Any]] = []
    readme = surface_root / "README.md"
    changelog = surface_root / "CHANGELOG.md"
    readme_version: str | None = None
    if readme.is_file():
        readme_version = _latest_version_token(readme.read_text(encoding="utf-8", errors="replace"))
    if readme.is_file() and changelog.is_file():
        changelog_text = changelog.read_text(encoding="utf-8", errors="replace")
        changelog_version = _latest_version_token(changelog_text)
        if readme_version and changelog_version and readme_version != changelog_version:
            if readme_version not in changelog_text.lower():
                findings.append(
                    {
                        "code": None,
                        "class": "version_readme_changelog_mismatch",
                        "path": _repo_relative(readme, repo_root),
                        "detail": f"README advertises {readme_version} not reflected in CHANGELOG",
                    }
                )
    schema_files = _iter_surface_files(
        surface_root, repo_root, name_match="open-dj.schema.json"
    )
    if readme_version and schema_files:
        for schema_path in schema_files:
            schema_text = schema_path.read_text(encoding="utf-8", errors="replace")
            schema_data = json.loads(schema_text)
            schema_id = str(schema_data.get("$id", ""))
            if readme_version.replace("v", "") not in schema_id and readme_version not in schema_id:
                findings.append(
                    {
                        "code": None,
                        "class": "version_schema_readme_mismatch",
                        "path": _repo_relative(schema_path, repo_root),
                        "detail": f"schema $id does not match README version {readme_version}",
                    }
                )
    return findings


def _host_resolves(host: str) -> bool:
    if not host:
        return False
    try:
        socket.getaddrinfo(host, None)
        return True
    except OSError:
        return False


_SEED_UNRESOLVED_ID_HOSTS = frozenset({"open-dj.org"})


def schema_id_policy(surface_root: Path, repo_root: Path) -> list[dict[str, Any]]:
    if not surface_root.is_dir():
        return []
    disclaimer_present = bool(
        _NAMING_AUTHORITY_RE.search(_surface_markdown_text(surface_root, repo_root))
    )
    findings: list[dict[str, Any]] = []
    for json_path in _iter_surface_files(surface_root, repo_root, suffix=".json"):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        schema_id = data.get("$id")
        if not isinstance(schema_id, str):
            continue
        parsed = urlparse(schema_id)
        if parsed.scheme not in ("http", "https"):
            continue
        host = parsed.hostname or ""
        if disclaimer_present:
            continue
        host_unresolves = not _host_resolves(host)
        seed_host = host.lower() in _SEED_UNRESOLVED_ID_HOSTS
        if not host_unresolves and not seed_host:
            continue
        detail = f"unresolved $id host {host}"
        if seed_host and not host_unresolves:
            detail = f"$id host {host} lacks naming-authority policy"
        findings.append(
            {
                "code": "B3",
                "class": "schema_id_host_unresolved",
                "path": _repo_relative(json_path, repo_root),
                "href": schema_id,
                "detail": detail,
            }
        )
    return findings


def _normalize_corpus_root(raw: str) -> str:
    cleaned = raw.rstrip("/").replace("\\", "/")
    if cleaned.endswith(".open-dj.json"):
        return cleaned.rsplit("/", 1)[0]
    return cleaned


def source_of_truth_unique(surface_root: Path, repo_root: Path) -> list[dict[str, Any]]:
    if not surface_root.is_dir():
        return []
    roots: set[str] = set()
    for corpus_dir in surface_root.glob("conformance/corpus-*"):
        if corpus_dir.is_dir():
            roots.add(_normalize_corpus_root(_repo_relative(corpus_dir, repo_root)))
    for case_file in _iter_surface_files(
        surface_root, repo_root, name_match="case-*.open-dj.json"
    ):
        roots.add(_normalize_corpus_root(_repo_relative(case_file.parent, repo_root)))
    for md_path in _iter_markdown_files(surface_root, repo_root):
        text = md_path.read_text(encoding="utf-8", errors="replace")
        for match in _CORPUS_CITE_RE.findall(text):
            roots.add(_normalize_corpus_root(match))
    if len(roots) < 2:
        return []
    detail = ", ".join(sorted(roots))
    return [
        {
            "code": "B5",
            "class": "competing_corpora",
            "path": _repo_relative(surface_root, repo_root),
            "detail": f"competing corpus roots: {detail}",
        }
    ]


def typed_public_surface(surface_root: Path, repo_root: Path) -> list[dict[str, Any]]:
    if not surface_root.is_dir():
        return [
            {
                "code": None,
                "class": "probe_unscorable",
                "path": str(surface_root),
                "detail": "surface root missing",
            }
        ]
    py_typed = surface_root / "py.typed"
    if py_typed.is_file():
        return []
    for candidate in _iter_surface_files(surface_root, repo_root, name_match="py.typed"):
        if candidate.is_file():
            return []
    return [
        {
            "code": None,
            "class": "missing_py_typed",
            "path": _repo_relative(surface_root, repo_root),
            "detail": "py.typed marker missing",
        }
    ]


def format_doc_beside_parser(surface_root: Path, repo_root: Path) -> list[dict[str, Any]]:
    if not surface_root.is_dir():
        return []
    findings: list[dict[str, Any]] = []
    vendor_dirs = sorted(
        p for p in surface_root.iterdir() if p.is_dir() and not p.name.startswith("_")
    )
    if not vendor_dirs:
        return [
            {
                "code": None,
                "class": "probe_unscorable",
                "path": _repo_relative(surface_root, repo_root),
                "detail": "no vendor packages found",
            }
        ]
    for vendor_dir in vendor_dirs:
        if vendor_dir.name == "__pycache__":
            continue
        md_files = list(vendor_dir.glob("*.md"))
        if md_files:
            continue
        findings.append(
            {
                "code": None,
                "class": "missing_format_doc",
                "path": _repo_relative(vendor_dir, repo_root),
                "detail": f"no format documentation markdown for {vendor_dir.name}",
            }
        )
    return findings


def types_generated_contract(surface_root: Path, repo_root: Path) -> list[dict[str, Any]]:
    if not surface_root.is_dir():
        return [
            {
                "code": None,
                "class": "probe_unscorable",
                "path": str(surface_root),
                "detail": "surface root missing",
            }
        ]
    api_types = surface_root / "src" / "lib" / "api-types.ts"
    openapi = repo_root / "apps" / "webui" / "openapi.json"
    missing: list[str] = []
    if not api_types.is_file():
        missing.append("src/lib/api-types.ts")
    elif "generated" not in api_types.read_text(encoding="utf-8", errors="replace").lower():
        missing.append("generated marker in api-types.ts")
    if not openapi.is_file():
        missing.append("apps/webui/openapi.json")
    if not missing:
        return []
    return [
        {
            "code": None,
            "class": "generated_types_missing",
            "path": _repo_relative(surface_root, repo_root),
            "detail": f"missing generated contract artefacts: {', '.join(missing)}",
        }
    ]


PROBE_REGISTRY: dict[str, Callable[[Path, Path], list[dict[str, Any]]]] = {
    "xref_integrity": xref_integrity,
    "citation_resolves": citation_resolves,
    "docs_tree_agreement": docs_tree_agreement,
    "version_drift": version_drift,
    "schema_id_policy": schema_id_policy,
    "source_of_truth_unique": source_of_truth_unique,
    "typed_public_surface": typed_public_surface,
    "format_doc_beside_parser": format_doc_beside_parser,
    "types_generated_contract": types_generated_contract,
}


def run_probe(probe_name: str, surface_root: Path, repo_root: Path) -> list[dict[str, Any]]:
    probe = PROBE_REGISTRY.get(probe_name)
    if probe is None:
        raise KeyError(f"unknown probe: {probe_name}")
    return probe(surface_root, repo_root)
