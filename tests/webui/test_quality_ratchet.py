"""GET /api/v1/admin/quality-ratchet.

Regression lines:
- if the endpoint stops returning generated + metrics from the real baseline
  file then broken (the admin panel's quality section goes blank)
- if a missing baseline 200s instead of 503 then broken (silent empty section)
- if an unparseable baseline 200s instead of 500 then broken
- if a baseline missing 'generated' or 'metrics' 200s then broken
- if the response leaks the 'burn_down' narrative then the endpoint contract
  drifts from what it documents (generated + metrics only)
"""
from __future__ import annotations

import json

import pytest

from apps.webui.server.routes import quality


def test_quality_ratchet_served_from_repo(client):
    r = client.get("/api/v1/admin/quality-ratchet")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"generated", "metrics"}
    assert body["generated"]
    assert body["metrics"], "quality baseline served with no metrics"
    for key, value in body["metrics"].items():
        assert isinstance(value, (int, float)), key


def test_missing_baseline_is_loud(client, monkeypatch, tmp_path):
    monkeypatch.setattr(quality, "QUALITY_BASELINE_FILE", tmp_path / "nope.json")
    r = client.get("/api/v1/admin/quality-ratchet")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "quality_baseline_missing"


def test_unparseable_baseline_is_loud(client, monkeypatch, tmp_path):
    broken = tmp_path / "baseline.json"
    broken.write_text("{not json")
    monkeypatch.setattr(quality, "QUALITY_BASELINE_FILE", broken)
    r = client.get("/api/v1/admin/quality-ratchet")
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "quality_baseline_unparseable"


def test_baseline_without_metrics_key_is_loud(client, monkeypatch, tmp_path):
    partial = tmp_path / "baseline.json"
    partial.write_text(json.dumps({"generated": "t"}))
    monkeypatch.setattr(quality, "QUALITY_BASELINE_FILE", partial)
    r = client.get("/api/v1/admin/quality-ratchet")
    assert r.status_code == 500
    assert r.json()["detail"]["message"].endswith("missing 'metrics'")


def test_baseline_without_generated_key_is_loud(client, monkeypatch, tmp_path):
    partial = tmp_path / "baseline.json"
    partial.write_text(json.dumps({"metrics": {}}))
    monkeypatch.setattr(quality, "QUALITY_BASELINE_FILE", partial)
    r = client.get("/api/v1/admin/quality-ratchet")
    assert r.status_code == 500
    assert r.json()["detail"]["message"].endswith("missing 'generated'")


def test_burn_down_narrative_is_not_leaked(client, monkeypatch, tmp_path):
    """The endpoint's contract is generated + metrics only, never the prose."""
    full = tmp_path / "baseline.json"
    full.write_text(json.dumps({
        "generated": "t",
        "metrics": {"ruff.total": 5},
        "burn_down": {"SOME_TRAIN": {"what": "internal commentary"}},
    }))
    monkeypatch.setattr(quality, "QUALITY_BASELINE_FILE", full)
    r = client.get("/api/v1/admin/quality-ratchet")
    assert r.status_code == 200
    assert set(r.json()) == {"generated", "metrics"}

pytestmark = pytest.mark.rb_parity
