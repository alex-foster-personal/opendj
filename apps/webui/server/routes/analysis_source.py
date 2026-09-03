"""PARITY-02: in-app rbx-vs-own analysis source toggle, agent-native.

GET  /api/v1/analysis-source  -> current per-feature selection
PUT  /api/v1/analysis-source  -> set one feature's source

Backed by :class:`apps.webui.server.analysis_source.AnalysisSourceStore`
(``app.state.analysis_source``), which is in-memory only and never
persisted -- see that module's docstring. This router is the ONE source of
truth the TopBar dropdown reads and writes; an agent driving this endpoint
directly gets the identical effect as clicking the UI control, satisfying
PARITY-02's agent-native-parity clause.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..analysis_source import FEATURES, AnalysisSource

router = APIRouter(prefix="/analysis-source", tags=["analysis-source"])


class AnalysisSourceOut(BaseModel):
    features: dict[str, AnalysisSource]


class AnalysisSourceSet(BaseModel):
    feature: str
    source: AnalysisSource


def _unknown_feature(feature: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "ANALYSIS_SOURCE_FEATURE_NOT_FOUND",
            "message": f"{feature!r} is not a toggleable feature; known: {list(FEATURES)}",
        },
    )


@router.get("", response_model=AnalysisSourceOut)
def get_analysis_source(request: Request) -> AnalysisSourceOut:
    return AnalysisSourceOut(features=request.app.state.analysis_source.snapshot())


@router.put("", response_model=AnalysisSourceOut)
def put_analysis_source(body: AnalysisSourceSet, request: Request) -> AnalysisSourceOut:
    if body.feature not in FEATURES:
        raise _unknown_feature(body.feature)
    features = request.app.state.analysis_source.set(body.feature, body.source)
    return AnalysisSourceOut(features=features)


__all__ = ["AnalysisSourceOut", "AnalysisSourceSet", "router"]
