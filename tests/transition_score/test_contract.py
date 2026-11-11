"""#7708: transition_score's closed contract keeps the wire documents, and http.build_service reaches try_pair."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.autodj.http import build_service as build_autodj
from apps.transition_score import tryer
from apps.transition_score.contract import (
    SCORER_VERSION,
    Candidate,
    Measured,
    ScoreResult,
    TransitionScoreService,
    TryResult,
    Veto,
)
from apps.transition_score.http import build_service

PLAN = {"style": "long_blend", "bars": 16, "out": {"deck": 1}}
METRICS = {"stretch_pct": 1.5, "in_trim_db": -1.0}
FACTORS = {"alignment": 0.9, "seam_clicks": 0.8}
UNKNOWN = {"vocal_overlap": "no stems"}

CANDIDATE_KEYS = [
    "id",
    "energy",
    "plan",
    "plan_sha256",
    "style",
    "bars",
    "metrics",
    "factors",
    "vetoes",
    "measured",
    "unknown",
    "score",
    "scorer_version",
    "render_ms",
    "score_ms",
    "refused",
    "render_path",
    "rank_group",
]


def _candidate() -> Candidate:
    return Candidate(
        id="c1",
        plan_json=json.dumps(PLAN),
        plan_sha256="0" * 64,
        style="long_blend",
        bars=16,
        metrics_json=json.dumps(METRICS),
        factors_json=json.dumps(FACTORS),
        vetoes=(Veto("clipping", 0.5, -1.0),),
        measured=Measured(k=5, n=7),
        unknown_json=json.dumps(UNKNOWN),
        score=0.0,
        scorer_version=SCORER_VERSION,
        render_ms=10.0,
        score_ms=2.0,
        render_path="mix.wav",
    )


def test_candidate_to_json_keeps_every_key_value_and_order() -> None:
    """[if] a fully populated Candidate serialises [then] the asdict-era document comes back key for key and in
    order with rank_group last, [else stop]."""
    document = _candidate().to_json()
    assert list(document) == CANDIDATE_KEYS, "if Candidate.to_json reorders keys then the try wire format drifts"
    assert document["plan"] == PLAN and document["metrics"] == METRICS
    assert document["factors"] == FACTORS and document["unknown"] == UNKNOWN
    assert document["vetoes"] == [{"name": "clipping", "value": 0.5, "threshold": -1.0}]
    assert document["measured"] == {"k": 5, "n": 7} and document["rank_group"] == "measured"


def test_mutating_a_read_leaves_the_candidate_unchanged() -> None:
    """[if] a caller mutates the dict a property returned [then] the next read is unchanged, [else stop]."""
    candidate = _candidate()
    candidate.metrics["stretch_pct"] = 99.0
    candidate.plan["style"] = "cut"
    assert candidate.metrics == METRICS and candidate.plan == PLAN, "if a read aliases state then the contract is open"


def test_score_result_to_json_keeps_key_order() -> None:
    """[if] a ScoreResult serialises [then] its keys and values are as before, [else stop]."""
    result = ScoreResult(
        metrics_json=json.dumps(METRICS),
        factors_json=json.dumps(FACTORS),
        vetoes=(),
        measured=Measured(k=4, n=7),
        unknown_json=json.dumps(UNKNOWN),
        score=0.5,
    )
    assert result.to_json() == {
        "metrics": METRICS,
        "factors": FACTORS,
        "vetoes": [],
        "measured": {"k": 4, "n": 7},
        "unknown": UNKNOWN,
        "score": 0.5,
        "scorer_version": SCORER_VERSION,
    }
    assert list(result.to_json()) == ["metrics", "factors", "vetoes", "measured", "unknown", "score", "scorer_version"]


def test_build_service_forwards_to_try_pair_with_its_bound_source_and_autodj(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the service's try_pair runs [then] tryer.try_pair gets every argument plus the bound source and
    autodj, [else stop]."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    answer = TryResult("a", "b", ())

    def recorder(*args: Any, **kwargs: Any) -> TryResult:
        calls.append((args, kwargs))
        return answer

    monkeypatch.setattr(tryer, "try_pair", recorder)
    autodj, source = build_autodj(), object()
    service = build_service(autodj, source)  # type: ignore[arg-type]
    assert isinstance(service, TransitionScoreService)
    got = service.try_pair(
        "a", "b", n=3, context_bars=2, data_dir=Path("d"), binary=Path("bin"), keep_renders_in=Path("k")
    )
    assert got is answer
    assert calls == [
        (
            ("a", "b"),
            {
                "n": 3,
                "context_bars": 2,
                "data_dir": Path("d"),
                "source": source,
                "binary": Path("bin"),
                "keep_renders_in": Path("k"),
                "autodj": autodj,
            },
        )
    ], "if build_service drops an argument then the try-er runs a different pair setup"


_HTTP_WITHOUT_AUDIO_STACK = """
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("soundfile", "scipy"):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
sys.meta_path.insert(0, Block())
import apps.transition_score.http as http
http.try_pair, http.plan_live, http.build_lanes
assert "scipy" not in sys.modules, "eager"
try:
    http.score_render
except ModuleNotFoundError as error:
    print("LAZY", error.name)
"""


def test_http_imports_without_scipy_and_loads_the_scorer_lazily() -> None:
    """[if] apps.transition_score.http is imported with scipy blocked [then] it imports, and only reading
    score_render names scipy, [else stop]."""
    done = subprocess.run(
        [sys.executable, "-c", _HTTP_WITHOUT_AUDIO_STACK],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert done.returncode == 0, (
        "if importing apps.transition_score.http loads scipy then the engine without the analysis extra fails to boot: "
        + done.stderr[-2000:]
    )
    assert "LAZY scipy" in done.stdout, done.stdout
