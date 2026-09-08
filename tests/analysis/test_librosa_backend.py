"""Portable librosa default backend contracts (META-01).

Salvaged from PR #412 (commit cac8301d): the ``librosa`` backend must be
the production default everywhere, analyze real audio with only the PyPI
``analysis`` extra installed, and never fabricate downbeat capability it
does not have. The learned ``librosa+madmom`` backend stays explicit-only.
"""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.analysis import auto_cues, detect_bad_beatgrid, run, write_tags
from apps.analysis.backends import DEFAULT_BACKEND, NONSHIPPABLE_ENV, get_backend
from apps.analysis.backends.librosa import LibrosaBackend
from apps.analysis.record import AnalysisRecord

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

_GET_MADMOM_BACKEND_PROBE = """
import json
from apps.analysis.backends import get_backend
from apps.analysis.backends.base import BackendNonshippable
try:
    backend = get_backend("librosa+madmom")
    print(json.dumps({"ok": True, "name": backend.name}))
except BackendNonshippable as exc:
    print(json.dumps({"ok": False, "message": str(exc)}))
"""


def _resolve_madmom_backend_in_subprocess(env: dict[str, str]) -> dict[str, object]:
    """Exercise the real registry in its own process -- AGENTS.md's
    fail-closed test contract bans monkeypatching, so the flag must be a
    genuine subprocess environment variable, never an in-process patch."""
    proc = subprocess.run(
        [sys.executable, "-c", _GET_MADMOM_BACKEND_PROBE],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.requirement("META-01")
def test_every_production_default_uses_exported_librosa_constant() -> None:
    assert DEFAULT_BACKEND == "librosa"
    assert inspect.signature(run.run).parameters["backend_name"].default == DEFAULT_BACKEND
    assert run._parse_args(["--files", "track.wav"]).backend == DEFAULT_BACKEND
    assert auto_cues._parse_args([]).backend == DEFAULT_BACKEND
    assert detect_bad_beatgrid._parse_args([]).backend == DEFAULT_BACKEND
    assert write_tags._parse_args(["--files", "track.wav"]).backend == DEFAULT_BACKEND

    for module in (run, auto_cues, detect_bad_beatgrid, write_tags):
        source = inspect.getsource(module)
        assert 'default="librosa+madmom"' not in source


@pytest.mark.requirement("META-01")
def test_registry_exposes_real_librosa_default_and_explicit_combined_backend() -> None:
    assert get_backend(DEFAULT_BACKEND) is LibrosaBackend
    result = _resolve_madmom_backend_in_subprocess({**os.environ, NONSHIPPABLE_ENV: "1"})
    assert result == {"ok": True, "name": "librosa+madmom"}
    assert get_backend("mik").name == "mik"


@pytest.mark.requirement("NATIVE-08")
def test_registry_refuses_madmom_without_the_nonshippable_flag() -> None:
    env = {k: v for k, v in os.environ.items() if k != NONSHIPPABLE_ENV}
    result = _resolve_madmom_backend_in_subprocess(env)
    assert result["ok"] is False
    assert "CC BY-NC-SA" in str(result["message"])
    # control: the portable default is never gated by the same flag.
    assert get_backend(DEFAULT_BACKEND) is LibrosaBackend


@pytest.mark.requirement("META-01")
def test_librosa_default_analyzes_real_click_without_fabricated_downbeats(
    click_120_path: Path,
) -> None:
    record = LibrosaBackend.analyze(click_120_path, "librosa-120")

    assert isinstance(record, AnalysisRecord)
    assert record.backend == DEFAULT_BACKEND
    assert record.backend_version.startswith("librosa==")
    assert "+numpy==" in record.backend_version
    assert "+scipy==" in record.backend_version
    assert any(abs(record.bpm - bpm) < 3.0 for bpm in (60.0, 120.0, 240.0))
    assert record.onsets_s
    assert record.key_camelot and record.key_openkey
    assert 1 <= record.energy <= 10
    assert record.downbeats_s == []
    assert record.features_blob["downbeat_tracking"] is False


@pytest.mark.requirement("META-02")
@pytest.mark.requirement("META-04")
def test_downstream_consumers_do_not_claim_absent_downbeat_capability(
    click_120_path: Path,
) -> None:
    record = LibrosaBackend.analyze(click_120_path, "librosa-downstream")

    proposal = auto_cues.propose_cues(record)
    assert all(cue.time_s in record.onsets_s for cue in proposal.cues)
    flag = detect_bad_beatgrid.detect_one(record)
    assert detect_bad_beatgrid.REASON_DOWNBEAT not in flag.reasons
