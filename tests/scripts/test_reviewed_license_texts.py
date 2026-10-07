"""OSSPUB-05: reviewed texts cover measured textless lock components.

[if] a measured textless lock package has no reviewed text [then] this file goes red, [else stop].
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts.license_mirrors import KNOWN_TEXTLESS, REVIEWED_LICENSE_TEXTS
from scripts.license_model import Component
from scripts.third_party_licenses import (
    LicenseInventoryError,
    attach_reviewed_license_texts,
    require_license_texts,
)

pytestmark = pytest.mark.requirement("OSSPUB-05")

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "license-measure-5189"
MANIFEST = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))


def _verified_json(name: str) -> list[dict]:
    entry = MANIFEST["files"][name]
    raw = (FIXTURES / name).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == entry["sha256"], f"{name} drifted from its pinned capture"
    assert len(raw) == entry["bytes"], f"{name} size drifted from the manifest"
    payload = json.loads(raw.decode("utf-8"))
    assert isinstance(payload, list)
    return payload


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _textless_records(ecosystem: str, name: str) -> list[dict]:
    filename = {
        "python": "python-textless.json",
        "javascript": "js-textless.json",
        "rust": "rust-textless.json",
    }[ecosystem]
    return [row for row in _verified_json(filename) if row["name"] == name]


def _component(ecosystem: str, record: dict, *, version: str | None = None) -> Component:
    return Component(
        ecosystem,
        record["name"],
        version if version is not None else record["version"],
        record["license"],
        texts=[],
    )


def _measured_textless() -> frozenset[tuple[str, str, str]]:
    """Textless lock rows that still need a reviewed text (KNOWN_TEXTLESS stays exempt)."""
    rows: set[tuple[str, str, str]] = set()
    for ecosystem, filename in (
        ("python", "python-textless.json"),
        ("javascript", "js-textless.json"),
        ("rust", "rust-textless.json"),
    ):
        for record in _verified_json(filename):
            if (ecosystem, record["name"]) in KNOWN_TEXTLESS:
                continue
            rows.add((ecosystem, record["name"], record["version"]))
    return frozenset(rows)


def test_every_reviewed_text_matches_its_pinned_sha256() -> None:
    for entry in REVIEWED_LICENSE_TEXTS:
        path = REPO_ROOT / entry.text_relative
        assert path.is_file(), entry.text_relative
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == entry.sha256, f"{entry.name} {entry.version} {entry.text_relative}"


def test_reviewed_texts_cover_the_measured_textless_lock_set() -> None:
    pinned = {(_norm(e.name), e.version, e.ecosystem) for e in REVIEWED_LICENSE_TEXTS}
    missing = [
        f"{eco}:{name} {version}"
        for eco, name, version in sorted(_measured_textless())
        if (_norm(name), version, eco) not in pinned
    ]
    assert missing == [], f"measured textless lock packages have no reviewed text: {missing}"


def test_python_textless_rows_match_the_deduped_inventory() -> None:
    all_rows = _verified_json("python-all-deduped.json")
    textless = _verified_json("python-textless.json")
    from_all = {(row["name"], row["version"]) for row in all_rows if not row["texts"]}
    from_textless = {(row["name"], row["version"]) for row in textless}
    assert from_all == from_textless


def test_attach_reviewed_fills_javascript_and_rust_components() -> None:
    components = [
        _component("javascript", _textless_records("javascript", "sirv")[0]),
        _component("rust", _textless_records("rust", "dasp_envelope")[0]),
        _component("python", _textless_records("python", "antlr4-python3-runtime")[0]),
        _component("python", _textless_records("python", "pyobjc-core")[0]),
    ]
    attached = attach_reviewed_license_texts(REPO_ROOT, components)
    for component in attached:
        assert component.license_texts, component.name
        assert any(text.strip() for _, text in component.license_texts)
    require_license_texts(attached)


def test_reviewed_mit_texts_keep_locked_revision_attribution() -> None:
    """Sol P1 (PR #5234 r4174715763): SPDX template copyright is not package attribution."""
    cases = (
        ("javascript", "is-reference", "Rich Harris"),
        ("javascript", "locate-character", "Rich Harris"),
        ("rust", "realfft", "HEnquist"),
        ("rust", "objc2", "Mads Marquart"),
        ("rust", "selectors", "The Servo Project Developers"),
    )
    for ecosystem, name, attribution in cases:
        (record,) = _textless_records(ecosystem, name)
        (component,) = attach_reviewed_license_texts(REPO_ROOT, [_component(ecosystem, record)])
        assert component.license_texts, name
        body = "\n".join(text for _, text in component.license_texts)
        assert attribution in body, name
        assert "<year>" not in body, name
        assert "<copyright holders>" not in body, name
    require_license_texts(
        attach_reviewed_license_texts(
            REPO_ROOT,
            [_component(ecosystem, _textless_records(ecosystem, name)[0]) for ecosystem, name, _ in cases],
        )
    )


def test_attach_reviewed_does_not_cover_a_mutated_captured_version() -> None:
    (rbox,) = _textless_records("python", "rbox")
    mutated = _component("python", rbox, version="0.1.8")
    (component,) = attach_reviewed_license_texts(REPO_ROOT, [mutated])
    assert component.license_texts == []
    with pytest.raises(LicenseInventoryError, match=re.escape("rbox 0.1.8")):
        require_license_texts([component])
