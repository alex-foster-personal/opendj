"""Per-track beatgrid source for ``/anlz``, split from rb_assets (quality file-size ratchet)."""

from __future__ import annotations

from fastapi import Request

from apps.analysis import selection

from . import analysis_source as analysis_source_routes

# The `/anlz` wire contract's `beatgrid_source` predates PARITY-02's lane
# selection module and uses "rekordbox"/"own"; `apps.analysis.selection`
# uses "rbx"/"own". Translate at the boundary rather than widen the wire
# vocabulary to match an internal module.
_BEATGRID_SOURCE_LABELS: dict[str, str] = {"rbx": "rekordbox", "own": "own"}


# `beatgrid_source_basis` on `/anlz`: WHY this track resolved to the source
# `beatgrid_source` names. "unmapped-default" is STANDALONE-06's per-track own
# (an unmapped track, the PARITY-02 toggle unset, the lane default still rbx),
# which legitimately differs from the lane-wide selection GET
# /api/v1/analysis/source reports. The client compares every payload against
# that lane-wide selection to reject stragglers from a source switch, so
# without this field it read every unmapped track as a straggler and refetched
# it forever (deck load never settled, trunk red Fri 25 Sep 2026).
BASIS_SELECTION = "selection"
BASIS_UNMAPPED_DEFAULT = "unmapped-default"


def beatgrid_source_for_track(request: Request, *, has_rb_mapping: bool) -> tuple[str, str]:
    """Per-track beatgrid source label and its basis for ``/anlz`` (STANDALONE-06).

    Reads through :mod:`apps.analysis.selection` once per request, source and
    basis off the SAME connection so they cannot straddle a PUT; callers
    thread the returned pair through rescue branches and
    ``rb_assets._resolve_beatgrid_source`` without re-reading (ADR-0099).
    """
    conn = analysis_source_routes._open_ro(request)
    try:
        source = selection.effective_source_for_track(
            conn, "beatgrid", has_rb_mapping=has_rb_mapping
        )
        implicit = selection.implicit_own_default(conn, "beatgrid", has_rb_mapping=has_rb_mapping)
    finally:
        conn.close()
    basis = BASIS_UNMAPPED_DEFAULT if implicit else BASIS_SELECTION
    return _BEATGRID_SOURCE_LABELS[source], basis
