"""Guard PRICE-06: backer copy on README and reqs.json, never in the app.

[if] README loses the solicitation or the app gains it [then] PRICE-06 is no longer a website-and-README placement rule, [else stop].
"""

from __future__ import annotations

import json
import pathlib

import pytest

pytestmark = pytest.mark.requirement("PRICE-06")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
REQS_JSON = REPO_ROOT / "reqs.json"
APPS = REPO_ROOT / "apps"

SOLICITATION = "Looking for financial backers"
PILLARS = (
    "Understand your library",
    "Play the set",
    "Plan a transition",
    "Keep the data yours",
)
SKIP_DIR_NAMES = frozenset(
    {".git", ".venv", "__pycache__", "node_modules", "dist", "build", ".svelte-kit"}
)


@pytest.fixture(scope="module")
def readme_text() -> str:
    assert README.is_file(), f"missing README: {README}"
    return README.read_text(encoding="utf-8")


def _walk_reqs(node: object) -> list[dict]:
    found: list[dict] = []
    if isinstance(node, dict):
        if node.get("id") == "PRICE-06":
            found.append(node)
        for value in node.values():
            found.extend(_walk_reqs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_reqs(item))
    return found


def test_readme_solicits_backers(readme_text: str) -> None:
    assert SOLICITATION in readme_text


def test_readme_points_at_site_and_privacy(readme_text: str) -> None:
    assert "https://open-dj.com" in readme_text
    assert "https://open-dj.com/privacy" in readme_text


def test_readme_has_four_pillar_headings(readme_text: str) -> None:
    for heading in PILLARS:
        assert heading in readme_text, f"missing pillar heading: {heading!r}"


def test_readme_hotlinks_performance_still(readme_text: str) -> None:
    assert "open-dj.com" in readme_text
    assert "odj-performance-4deck.jpg" in readme_text


def test_reqs_json_price06_is_shipped() -> None:
    payload = json.loads(REQS_JSON.read_text(encoding="utf-8"))
    rows = _walk_reqs(payload)
    assert len(rows) == 1, f"expected one PRICE-06 row, got {len(rows)}"
    assert rows[0]["status"] == "shipped"


def test_apps_do_not_solicit_backers() -> None:
    hits: list[str] = []
    for path in APPS.rglob("*"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if SOLICITATION in text:
            hits.append(path.relative_to(REPO_ROOT).as_posix())
    assert hits == []
