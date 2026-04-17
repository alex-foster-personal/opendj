"""Fixtures for tests/sync/*. Re-exports djay fixtures from tests.sets.conftest."""
from __future__ import annotations

# Re-export the djay fixture so tests/sync tests can use it without
# duplicating the (substantial) TSAF-blob builder.
from tests.sets.conftest import (  # noqa: F401 -- fixture re-export
    djay_fixture_factory,
    djay_three_tracks_two_decks,
)
