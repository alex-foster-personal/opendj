"""Stub LLM explainer -- canned text for --explain (Plan 03 §5.3).

Swapping this for an Ollama / llama.cpp subprocess consumer is a
drop-in: same single-function surface, same return string. Lives in
Phase 13 only so the hot path never depends on an LLM being reachable.
"""
from __future__ import annotations

from .rank_stage1 import ScoredCandidate


def explain(candidate: ScoredCandidate) -> str:
    """Emit a short, LLM-shaped explanation from candidate rationale."""
    parts: list[str] = []
    r = candidate.rationale
    if r.get("camelot", 0.0) >= 0.9:
        parts.append("harmonic match")
    elif r.get("camelot", 0.0) >= 0.7:
        parts.append("adjacent key")
    if r.get("bpm", 0.0) >= 0.9:
        parts.append("BPM locked")
    elif r.get("bpm", 0.0) >= 0.6:
        parts.append("BPM in range")
    for tag in ("pair_manual", "pair_learned", "pair_ai"):
        if r.get(tag):
            parts.append(
                {
                    "pair_manual": "manual pair",
                    "pair_learned": "learned pair",
                    "pair_ai": "AI pair",
                }[tag]
            )
            break
    if not parts:
        parts.append("baseline compatibility")
    return ", ".join(parts)
