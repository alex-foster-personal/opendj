"""The play_analytics contract: service satisfies the Protocol, models are strict."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.play_analytics.contract import (
    CONTRACT_VERSION,
    PlayAnalyticsFilters,
    PlayAnalyticsReadModel,
    PlayAnalyticsService,
)
from apps.play_analytics.http import build_service
from apps.play_analytics.query import query_play_analytics


def test_service_satisfies_protocol_and_matches_query(analytics_db: Path) -> None:
    service: PlayAnalyticsService = build_service()
    assert isinstance(service, PlayAnalyticsService)
    assert CONTRACT_VERSION == "play_analytics/2"
    payload = service.query_play_analytics(analytics_db, limit=50)
    assert payload == query_play_analytics(analytics_db, limit=50)
    model = PlayAnalyticsReadModel.model_validate(payload)
    assert model.model_dump(mode="json") == payload  # the wire is JSON: a tuple is a list


def test_contract_models_reject_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PlayAnalyticsFilters(share_state=None, limit=1, min_audible_s=0.0, extra=1)
