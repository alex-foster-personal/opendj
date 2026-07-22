"""Structural tests for `mkdocs.yml`.

These tests are deliberately dependency-free: they parse `mkdocs.yml` as YAML
and verify every nav entry resolves to a file on disk. The heavier
`test_mkdocs_build.py` exercises the real mkdocs toolchain.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"


def _load_config() -> dict:
    assert MKDOCS_YML.exists(), f"missing {MKDOCS_YML}"
    with MKDOCS_YML.open("r", encoding="utf-8") as fh:
        # mkdocs uses custom !!python tags sometimes; for our minimal config
        # yaml.safe_load is sufficient.
        return yaml.safe_load(fh)


def _collect_nav_paths(nav_entry) -> list[str]:
    """Flatten a mkdocs `nav:` tree into a list of leaf file paths."""
    out: list[str] = []
    if isinstance(nav_entry, list):
        for item in nav_entry:
            out.extend(_collect_nav_paths(item))
    elif isinstance(nav_entry, dict):
        for value in nav_entry.values():
            if isinstance(value, str):
                out.append(value)
            else:
                out.extend(_collect_nav_paths(value))
    elif isinstance(nav_entry, str):
        out.append(nav_entry)
    return out


def test_mkdocs_yml_parses():
    cfg = _load_config()
    assert cfg["site_name"] == "open-dj"
    assert cfg["docs_dir"] == "open-dj"
    assert "nav" in cfg and cfg["nav"], "nav must be non-empty"


def test_theme_is_material():
    cfg = _load_config()
    assert cfg["theme"]["name"] == "material"


def test_every_nav_path_exists_on_disk():
    cfg = _load_config()
    docs_dir = REPO_ROOT / cfg["docs_dir"]
    missing = []
    for rel in _collect_nav_paths(cfg["nav"]):
        candidate = docs_dir / rel
        if not candidate.is_file():
            missing.append(rel)
    assert not missing, f"nav entries not found under {docs_dir}: {missing}"


def test_nav_has_expected_top_sections():
    cfg = _load_config()
    top = []
    for entry in cfg["nav"]:
        if isinstance(entry, dict):
            top.extend(entry.keys())
        else:
            top.append(str(entry))
    for required in ("Home", "Spec v0.2", "Adapters", "Conformance", "Contributing"):
        assert required in top, f"missing top-level nav section: {required}"


def test_markdown_extensions_include_admonition_and_superfences():
    cfg = _load_config()
    exts = cfg.get("markdown_extensions", [])
    # extensions may be strings or single-key dicts
    names = []
    for ext in exts:
        if isinstance(ext, str):
            names.append(ext)
        elif isinstance(ext, dict):
            names.extend(ext.keys())
    assert "admonition" in names
    assert "pymdownx.superfences" in names
    assert "toc" in names


def test_requirements_docs_file_present():
    req = REPO_ROOT / "requirements-docs.txt"
    assert req.is_file(), "requirements-docs.txt missing at repo root"
    text = req.read_text(encoding="utf-8")
    assert "mkdocs" in text
    assert "mkdocs-material" in text
    assert "pymdown-extensions" in text


def test_docs_workflow_present():
    wf = REPO_ROOT / ".github" / "workflows" / "docs.yml"
    assert wf.is_file(), ".github/workflows/docs.yml missing"
    text = wf.read_text(encoding="utf-8")
    assert "mkdocs build --strict" in text
    assert "actions/deploy-pages" in text


def test_ci_and_docs_workflows_target_integration_branch():
    """CI must run for commits and PRs targeting the live integration branch."""
    for workflow_name in ("ci.yml", "docs.yml"):
        workflow = REPO_ROOT / ".github" / "workflows" / workflow_name
        text = workflow.read_text(encoding="utf-8")
        assert text.count("branches: [af--rekordbox-parity-ui]") == 2


def test_docs_publish_guards_target_integration_branch():
    workflow = REPO_ROOT / ".github" / "workflows" / "docs.yml"
    text = workflow.read_text(encoding="utf-8")

    assert text.count("github.ref == 'refs/heads/af--rekordbox-parity-ui'") == 2
    assert "github.ref == 'refs/heads/master'" not in text


@pytest.mark.parametrize(
    "script",
    ["scripts/build-spec-site.py"],
)
def test_helper_scripts_present(script: str):
    path = REPO_ROOT / script
    assert path.is_file(), f"{script} missing"
    assert "mkdocs" in path.read_text(encoding="utf-8")
