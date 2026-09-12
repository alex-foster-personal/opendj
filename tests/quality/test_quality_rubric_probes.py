"""Unit tests for quality rubric probes on miniature surfaces."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from scripts.quality_rubric_model import load_rubric
from scripts.quality_rubric_probes import (
    citation_resolves,
    schema_id_policy,
    source_of_truth_unique,
    xref_integrity,
)
from scripts.quality_rubric_score import score_dimension

REPO = Path(__file__).resolve().parents[2]
RUBRIC = load_rubric()


def _dimension(dimension_id: str):
    for dimension in RUBRIC.dimensions:
        if dimension.id == dimension_id:
            return dimension
    raise KeyError(dimension_id)


def test_xref_integrity_flags_b1_escape(tmp_path: Path) -> None:
    (tmp_path / "inside.md").write_text("ok", encoding="utf-8")
    (tmp_path / "README.md").write_text("[x](../outside.md)\n", encoding="utf-8")
    findings = xref_integrity(tmp_path, REPO)
    codes = {f.get("code") for f in findings}
    assert "B1" in codes
    assert any(f["class"] == "relative_link_escapes_surface" for f in findings)


def test_citation_resolves_flags_b2_placeholder(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "See https://github.com/example/gpl-serato\n", encoding="utf-8"
    )
    findings = citation_resolves(tmp_path, REPO)
    assert any(f["code"] == "B2" and f["class"] == "citation_placeholder" for f in findings)


def test_schema_id_policy_flags_b3_invalid_host(tmp_path: Path) -> None:
    (tmp_path / "schema.json").write_text(
        '{"$id": "https://quality-rubric.invalid/s"}\n', encoding="utf-8"
    )
    findings = schema_id_policy(tmp_path, REPO)
    assert any(f["code"] == "B3" and f["class"] == "schema_id_host_unresolved" for f in findings)


def test_source_of_truth_unique_flags_b5_competing_corpora(tmp_path: Path) -> None:
    corpus = tmp_path / "conformance" / "corpus-0.2"
    corpus.mkdir(parents=True)
    (corpus / "case-01.open-dj.json").write_text("{}", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "Corpus also at tests/fixtures/conformance/\n", encoding="utf-8"
    )
    findings = source_of_truth_unique(tmp_path, REPO)
    assert any(f["code"] == "B5" and f["class"] == "competing_corpora" for f in findings)


def test_happy_path_probes_emit_no_b_codes(tmp_path: Path) -> None:
    (tmp_path / "other.md").write_text("linked\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("[ok](other.md)\n", encoding="utf-8")
    corpus = tmp_path / "conformance" / "corpus-0.2"
    corpus.mkdir(parents=True)
    (corpus / "case-01.open-dj.json").write_text("{}", encoding="utf-8")

    xref = xref_integrity(tmp_path, REPO)
    cite = citation_resolves(tmp_path, REPO)
    schema = schema_id_policy(tmp_path, REPO)
    corpus_findings = source_of_truth_unique(tmp_path, REPO)

    for findings in (xref, cite, schema, corpus_findings):
        assert not any(f.get("code") in {"B1", "B2", "B3", "B5"} for f in findings)

    assert score_dimension(_dimension("xref.integrity"), xref) == 5
    assert score_dimension(_dimension("citation.resolves"), cite) == 5
    assert score_dimension(_dimension("schema.id_policy"), schema) == 5
    assert score_dimension(_dimension("source_of_truth.unique"), corpus_findings) == 5


def test_xref_integrity_skips_node_modules_enametoolong(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("[x](missing.md)\n", encoding="utf-8")
    vendored = (
        tmp_path
        / "node_modules"
        / ".pnpm"
        / "semver@7.8.5"
        / "node_modules"
        / "semver"
    )
    vendored.mkdir(parents=True)
    # No ')' in the href: the markdown extractor stops at the first ')'.
    long_href = "=' | '='  partial-" + ("x" * 300)
    (vendored / "README.md").write_text(f"[bad]({long_href})\n", encoding="utf-8")
    with pytest.raises(OSError) as raised:
        (vendored / long_href).exists()
    assert raised.value.errno == errno.ENAMETOOLONG

    findings = xref_integrity(tmp_path, REPO)

    assert not any("node_modules" in str(f.get("path", "")) for f in findings)
    assert any(
        f["class"] == "relative_link_dangling"
        and Path(f["path"]).name == "README.md"
        and f.get("href") == "missing.md"
        for f in findings
    )
