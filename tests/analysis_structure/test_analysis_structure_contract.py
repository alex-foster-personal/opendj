"""The analysis_structure contract: the service satisfies its Protocol, models reject extras.

-Claude
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.analysis_structure.contract import (
    CONTRACT_VERSION,
    OwnPhrasesStatus,
    StructureService,
)
from apps.analysis_structure.http import build_service


def test_service_satisfies_protocol_and_delegates(tmp_path):
    service = build_service()
    assert isinstance(service, StructureService)
    assert CONTRACT_VERSION == "analysis_structure/1"
    # Cheap real call: rbx lane with vendor phrases is served untouched.
    out = service.merge_own_phrases({"phrases": [{"start_s": 0.0}]}, "s1", tmp_path, source="rbx")
    assert out["phrases_source"] == "rekordbox"
    out = service.merge_own_phrases({"phrases": []}, "absent", tmp_path, source="own")
    assert out == {"phrases": [], "phrases_source": "none"}
    with pytest.raises(ValueError):
        service.merge_own_phrases({}, "s1", tmp_path, source="djay")


def test_own_phrases_status_rejects_extra_fields_and_is_frozen():
    status = OwnPhrasesStatus(status="ok", producer="allin1")
    with pytest.raises(ValidationError):
        OwnPhrasesStatus(status="ok", unexpected=1)
    with pytest.raises(ValidationError):
        status.status = "failed"
