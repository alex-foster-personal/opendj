"""Tests for ``apps.tags.unify``."""
from __future__ import annotations

import pytest

from apps.shared.tag_writer import TagRead
from apps.tags.unify import (
    DEFAULT_POLICY,
    SOURCE_CONFIDENCE,
    TagSources,
    unify,
)


@pytest.mark.requirement("META-03")
def test_rb_wins_title_when_present() -> None:
    sources = TagSources(
        rb=TagRead(title="RB Title"),
        file=TagRead(title="File Title"),
    )
    plan = unify(sources)
    assert plan.tags.title == "RB Title"
    assert plan.provenance["title"].source == "rb"
    assert plan.provenance["title"].confidence == SOURCE_CONFIDENCE["rb"]


@pytest.mark.requirement("META-03")
def test_fallback_rb_empty_file_wins() -> None:
    sources = TagSources(
        rb=TagRead(title=None),
        file=TagRead(title="File Title"),
    )
    plan = unify(sources)
    assert plan.tags.title == "File Title"
    assert plan.provenance["title"].source == "file"


@pytest.mark.requirement("META-03")
def test_mik_wins_bpm_when_present() -> None:
    sources = TagSources(
        mik=TagRead(bpm=128.0),
        rb=TagRead(bpm=120.0),
    )
    plan = unify(sources)
    assert plan.tags.bpm == 128.0
    assert plan.provenance["bpm"].source == "mik"


@pytest.mark.requirement("META-03")
def test_sanity_rejects_bad_bpm_falls_through() -> None:
    sources = TagSources(
        mik=TagRead(bpm=0.0),  # invalid
        rb=TagRead(bpm=128.0),
    )
    plan = unify(sources)
    assert plan.tags.bpm == 128.0
    assert plan.provenance["bpm"].source == "rb"


@pytest.mark.requirement("META-03")
def test_sanity_rejects_out_of_range_energy() -> None:
    sources = TagSources(
        mik=TagRead(energy=12),  # invalid
        rb=TagRead(energy=7),
    )
    plan = unify(sources)
    assert plan.tags.energy == 7


@pytest.mark.requirement("META-03")
def test_deterministic_rerun() -> None:
    sources = TagSources(
        rb=TagRead(title="T", artist="A", bpm=128.0),
        file=TagRead(title="Old", energy=5),
    )
    a = unify(sources)
    b = unify(sources)
    # Provenance includes attempts log.
    assert a.tags == b.tags
    assert {k: v.source for k, v in a.provenance.items()} == {
        k: v.source for k, v in b.provenance.items()
    }


@pytest.mark.requirement("META-03")
def test_missing_everything_produces_empty_plan() -> None:
    plan = unify(TagSources())
    assert plan.tags.title is None
    assert plan.tags.artist is None
    assert plan.provenance == {}


@pytest.mark.requirement("META-03")
def test_provenance_records_all_attempts() -> None:
    sources = TagSources(
        rb=TagRead(title=None),
        file=TagRead(title="F"),
        filename=TagRead(title="FN"),
        djay=TagRead(title="D"),
    )
    plan = unify(sources)
    attempts = dict(plan.provenance["title"].attempts)
    # RB empty -> False; File had value -> True (winner); filename/djay not probed.
    assert attempts["rb"] is False
    assert attempts["file"] is True
