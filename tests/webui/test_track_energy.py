"""Characterization tests for Copilot/PLAY IT provenance energy projection."""

from __future__ import annotations

import pytest

from apps.webui.server.backend import Provenance, Track
from apps.webui.server.track_energy import energy_of

_STAMP = "2026-10-16T12:00:00Z"


def _track(**provenance: Provenance) -> Track:
    return Track(
        stable_id="t1",
        title="T",
        created_at=_STAMP,
        updated_at=_STAMP,
        provenance=dict(provenance),
    )


def test_energy_of_missing_envelope_returns_none():
    assert energy_of(_track()) is None


def test_energy_of_envelope_with_none_value_returns_none():
    prov = Provenance(None, "mik", 1.0, _STAMP, "ok")
    assert energy_of(_track(energy=prov)) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0),
        (7, 7),
        (10, 10),
        (11, 11),
        ("8", 8),
    ],
)
def test_energy_of_bare_int_coercion(raw: object, expected: int):
    prov = Provenance(raw, "mik", 1.0, _STAMP, "ok")
    assert energy_of(_track(energy=prov)) == expected
