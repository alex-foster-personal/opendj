"""ENRICH-12086: reversible Cyanite taxonomy map."""

from __future__ import annotations

import pytest

from apps.genre_infer import tag_map, taxonomy
from apps.genre_infer.providers import enrichment_layers_available


@pytest.mark.requirement("ENRICH-12086")
def test_cyanite_snapshot_has_thirty_two_categories_and_five_thousand_seventy_nine_tags():
    """[if] snapshot loads [then] 32 categories and 5079 tags are preserved, [else stop]."""
    loaded = tag_map.load_taxonomy_map()
    assert loaded.stats.category_count == 32
    assert loaded.stats.tag_count == 5079
    assert len(loaded.category_names()) == 32


@pytest.mark.requirement("ENRICH-12086")
def test_vendor_spellings_agressive_and_nonrythmic_map_explicitly():
    """[if] vendor tag is misspelled [then] raw spelling is kept and mapping is explicit, [else stop]."""
    loaded = tag_map.default_map()
    aggressive = loaded.resolve(vendor="cyanite", category="Simple Moods", vendor_value="Agressive")
    nonrythmic = loaded.resolve(vendor="cyanite", category="Movement", vendor_value="Nonrythmic")
    assert aggressive.vendor_value == "Agressive"
    assert aggressive.open_dj_id == "mood.aggressive"
    assert nonrythmic.vendor_value == "Nonrythmic"
    assert nonrythmic.open_dj_id == "movement.nonrhythmic"


@pytest.mark.requirement("ENRICH-12086")
def test_beatport_genre_unknown_is_never_silent_coerce():
    """[if] beatport name is absent [then] open_dj_id is unknown with raw value kept, [else stop]."""
    unknown = tag_map.resolve_beatport_genre("Not A Real Beatport Genre XYZ")
    assert unknown.vendor_value == "Not A Real Beatport Genre XYZ"
    assert unknown.open_dj_id == tag_map.OPEN_DJ_UNKNOWN
    known = tag_map.resolve_beatport_genre("House")
    assert known.open_dj_id == taxonomy.canonical("House")


@pytest.mark.requirement("ENRICH-12086")
def test_taxonomy_map_loads_without_vendor_credentials():
    """[if] no vendor account [then] default_map still loads for library workflows, [else stop]."""
    assert enrichment_layers_available() is True
    assert tag_map.default_map().stats.provider == "Cyanite"


@pytest.mark.requirement("ENRICH-12086")
def test_preserve_snapshot_keeps_all_categories():
    """[if] preserve_snapshot runs [then] no category from the fixture is dropped, [else stop]."""
    loaded = tag_map.load_taxonomy_map()
    preserved = loaded.preserve_snapshot()
    assert len(preserved["categories"]) == 32
    names = {c["name"] for c in preserved["categories"]}
    assert "Simple Moods" in names and "Movement" in names and "BPM" in names
