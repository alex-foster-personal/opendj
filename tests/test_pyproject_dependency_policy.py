"""pyproject.toml opens with the Python dependency policy block.

The block is the single place a reader (human or agent) learns that uv is the
only installer, pyproject.toml the only declaration and uv.lock the only lock
(ADR docs/decisions/ADR-*-uv-lock-single-python-lock.md). These tests pin that
it stays at the top, keeps its load-bearing rules, and cites only https URLs
on documentation hosts, so a link cannot quietly rot into a non-doc host.
"""

import re
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

POLICY_TITLE = "# Python dependency policy"
DOC_HOSTS = {"docs.astral.sh", "packaging.python.org", "peps.python.org"}
REQUIRED_RULES = (
    "uv is the only installer and resolver",
    "uv.lock is the committed lock",
    "uv sync --locked",
    "[dependency-groups] (PEP 735)",
    "PEP 723",
    "uv export",
)
REQUIRED_URLS = {
    "https://docs.astral.sh/uv/concepts/projects/sync/",
    "https://docs.astral.sh/uv/guides/integration/github/",
    "https://peps.python.org/pep-0735/",
    "https://peps.python.org/pep-0723/",
    "https://peps.python.org/pep-0751/",
}
_URL_RE = re.compile(r"\w+://\S+")


def _policy_block(text: str) -> list[str]:
    """Leading comment lines of pyproject.toml, up to the first non-comment line."""
    lines = text.splitlines()
    block: list[str] = []
    for line in lines:
        if not line.startswith("#"):
            break
        block.append(line)
    return block


def test_pyproject_starts_with_the_policy_block() -> None:
    block = _policy_block(PYPROJECT.read_text(encoding="utf-8"))
    assert len(block) >= 10, f"expected a leading comment block, got {len(block)} line(s)"
    assert any(line.startswith(POLICY_TITLE) for line in block[:3]), (
        f"pyproject.toml must open with '{POLICY_TITLE}' in its first 3 lines"
    )


def test_policy_block_states_every_load_bearing_rule() -> None:
    joined = "\n".join(_policy_block(PYPROJECT.read_text(encoding="utf-8")))
    missing = [rule for rule in REQUIRED_RULES if rule not in joined]
    assert not missing, f"policy block lost rule(s): {missing}"


def test_policy_urls_are_https_on_doc_hosts() -> None:
    joined = "\n".join(_policy_block(PYPROJECT.read_text(encoding="utf-8")))
    urls = set(_URL_RE.findall(joined))
    # Presence first: an empty set would pass the host check vacuously.
    assert urls >= REQUIRED_URLS, f"missing doc URL(s): {sorted(REQUIRED_URLS - urls)}"
    bad = sorted(
        url
        for url in urls
        if urlparse(url).scheme != "https" or urlparse(url).hostname not in DOC_HOSTS
    )
    assert not bad, f"non-https or off-allowlist URL(s) in policy block: {bad}"
