"""DJ labels from drums and bass energy (STRUCT-03).

Acceptance lines:
  [if] a track goes intro -> buildup -> drop -> breakdown -> buildup -> drop -> outro
       in its drums and bass energy [then] the sections are labeled exactly that
       ⛔️ pop labels, or every loud section called a drop regardless of order
  [if] the intro has no drums [then] it is labeled intro with ambient true
       ⛔️ an ambient intro indistinguishable from a drum intro
  [if] the runner wrote no envelopes [then] no section gets a DJ label and the
       reason is named ⛔️ labels guessed from the pop labels
  [if] the energy is flat across the track [then] it is unlabeled, never a drop
       everywhere ⛔️ drop on every section of a track with nothing to contrast
"""

from __future__ import annotations

from typing import Any

from apps.analysis_structure import dj_labels
from apps.analysis_structure.dj_labels import label_sections
from apps.analysis_structure import pipeline
from apps.analysis_structure.pipeline import build_sidecar, write_sidecar
from apps.analysis_structure.serve import merge_own_phrases

HOP = 0.1
ON, OFF, SILENT = -8.0, -40.0, -90.0  # absolute dBFS levels per lane


def _envelopes(plan: list[tuple[float, float, float, float, float | None]]) -> dict[str, Any]:
    """``plan``: (seconds, drums_db, bass_db, mix_db, mix_end_db or None for flat)."""
    env: dict[str, Any] = {"hop_s": HOP, "drums": [], "bass": [], "mix": []}
    for seconds, drums, bass, mix, mix_end in plan:
        n = round(seconds / HOP)
        env["drums"] += [drums] * n
        env["bass"] += [bass] * n
        end = mix if mix_end is None else mix_end
        env["mix"] += [round(mix + (end - mix) * i / max(1, n - 1), 2) for i in range(n)]
    return env


def _sections(lengths: list[float]) -> list[dict[str, Any]]:
    out, t = [], 0.0
    for length in lengths:
        out.append({"start_s": t, "end_s": t + length, "label": "inst"})
        t += length
    return out


# intro (drums+bass low-ish, not full), buildup (drums in, bass out), drop,
# breakdown (drums and bass out), buildup (mix rising, bass out), drop, outro.
CLASSIC = [
    (32.0, -20.0, -20.0, -18.0, None),  # intro: drums and bass half in
    (16.0, ON, OFF, -12.0, None),  # buildup: drums return without bass
    (64.0, ON, ON, -6.0, None),  # drop
    (32.0, OFF, OFF, -20.0, None),  # breakdown
    (16.0, OFF, OFF, -24.0, -10.0),  # buildup: mix rises
    (64.0, ON, ON, -6.0, None),  # drop
    (32.0, -20.0, -30.0, -18.0, None),  # outro
]


def _labels(result: dict[str, Any]) -> list[str]:
    return [s["dj_label"] for s in result["sections"]]


def test_classic_arc_is_labeled_intro_buildup_drop_breakdown_buildup_drop_outro():
    result = label_sections(_sections([p[0] for p in CLASSIC]), _envelopes(CLASSIC))
    assert result["status"] == "ok"
    assert _labels(result) == [
        "intro", "buildup", "drop", "breakdown", "buildup", "drop", "outro",
    ]  # fmt: skip
    assert not any(s["ambient"] for s in result["sections"])
    # Energies are carried per section so a label can be audited.
    drop = result["sections"][2]["energy"]
    assert drop["drums_db"] >= dj_labels.FULL_DB and drop["bass_db"] >= dj_labels.FULL_DB


def test_withdrawn_section_before_a_drop_that_does_not_rise_is_a_breakdown_not_a_buildup():
    plan = [*CLASSIC[:4], (16.0, OFF, OFF, -20.0, None), *CLASSIC[5:]]
    result = label_sections(_sections([p[0] for p in plan]), _envelopes(plan))
    assert _labels(result)[3:5] == ["breakdown", "breakdown"]


def test_withdrawn_sections_after_the_last_drop_are_cooldown_then_outro():
    plan = [*CLASSIC[:3], (32.0, OFF, OFF, -20.0, None), (32.0, -20.0, OFF, -22.0, None)]
    result = label_sections(_sections([p[0] for p in plan]), _envelopes(plan))
    assert _labels(result) == ["intro", "buildup", "drop", "cooldown", "outro"]


def test_drumless_intro_and_outro_are_ambient():
    plan = [(32.0, SILENT, OFF, -30.0, None), *CLASSIC[1:3], (32.0, SILENT, SILENT, -30.0, None)]
    result = label_sections(_sections([p[0] for p in plan]), _envelopes(plan))
    assert _labels(result) == ["intro", "buildup", "drop", "outro"]
    assert [s["ambient"] for s in result["sections"]] == [True, False, False, True]


def test_intro_with_drums_is_not_ambient():
    """Control for the ambient flag: it must be able to say no."""
    plan = [(32.0, ON, OFF, -12.0, None), *CLASSIC[2:3], (32.0, OFF, OFF, -30.0, None)]
    result = label_sections(_sections([p[0] for p in plan]), _envelopes(plan))
    assert result["sections"][0]["dj_label"] == "intro"
    assert result["sections"][0]["ambient"] is False


def test_missing_envelopes_leave_every_section_unlabeled():
    sections = _sections([32.0, 64.0])
    for env in (None, {}):
        result = label_sections(sections, env)
        assert result["status"] == "unlabeled" and result["reason"] == "no_envelopes"
        assert "sections" not in result


def test_flat_energy_track_does_not_get_a_drop_everywhere():
    plan = [(32.0, ON, ON, -6.0, None)] * 6
    result = label_sections(_sections([p[0] for p in plan]), _envelopes(plan))
    assert result["status"] == "unlabeled" and result["reason"] == "no_energy_contrast"


def test_mastering_level_does_not_change_the_labels():
    """Normalization control: the same arc 15 dB quieter gets the same labels."""
    quiet = [
        (s, d - 15, b - 15, m - 15, None if e is None else e - 15) for s, d, b, m, e in CLASSIC
    ]
    loud = label_sections(_sections([p[0] for p in CLASSIC]), _envelopes(CLASSIC))
    soft = label_sections(_sections([p[0] for p in quiet]), _envelopes(quiet))
    assert _labels(soft) == _labels(loud)


def test_a_track_with_no_bass_at_all_is_unlabeled_with_the_lane_named():
    plan = [(s, d, SILENT - 40, m, e) for s, d, _b, m, e in CLASSIC]
    result = label_sections(_sections([p[0] for p in plan]), _envelopes(plan))
    assert result["status"] == "unlabeled" and result["reason"] == "silent_lane:bass"


def test_section_past_the_end_of_the_envelope_is_unlabeled_not_guessed():
    env = _envelopes(CLASSIC)
    sections = [*_sections([p[0] for p in CLASSIC]), {"start_s": 400.0, "end_s": 420.0}]
    assert label_sections(sections, env)["reason"] == "section_outside_envelope"


def test_a_too_short_section_inside_the_envelope_is_named_as_too_short():
    env = _envelopes(CLASSIC)
    sections = _sections([p[0] for p in CLASSIC])
    first_end = float(sections[0]["end_s"])
    sections.insert(1, {"start_s": first_end, "end_s": first_end + 0.1})
    sections[2] = {**sections[2], "start_s": first_end + 0.1}
    assert label_sections(sections, env)["reason"] == "section_too_short"


GRID = [2.0 * i for i in range(160)]
GRID_ID = {"backend": "own_beatgrid.backfill", "backend_version": "1.0.0"}


def _raw(envelopes: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    t, segments = 0.0, []
    for seconds, *_ in CLASSIC:
        segments.append({"start": t, "end": t + seconds, "label": "inst"})
        t += seconds
    return {"status": "ok", "segments": segments, "envelopes": envelopes, **extra}


def test_sidecar_sections_keep_the_model_label_and_gain_dj_fields(tmp_path):
    doc = build_sidecar("s1", _raw(_envelopes(CLASSIC)), GRID, GRID_ID)
    assert doc["dj_labels"]["status"] == "ok"
    assert [s["label"] for s in doc["sections"]] == ["inst"] * 7
    assert [s["dj_label"] for s in doc["sections"]][2] == "drop"
    write_sidecar(tmp_path, doc)
    served = merge_own_phrases({"phrases": []}, "s1", tmp_path, source="rbx")
    phrase = served["phrases"][2]
    assert phrase["label"] == "drop" and phrase["model_label"] == "inst"
    assert phrase["ambient"] is False and phrase["source"] == "own"


def test_sidecar_without_envelopes_serves_the_model_label_and_names_why(tmp_path):
    raw = _raw(None, envelopes_error="LibsndfileError: boom")
    doc = build_sidecar("s1", raw, GRID, GRID_ID)
    assert doc["status"] == "ok"
    assert doc["dj_labels"]["status"] == "unlabeled"
    assert doc["dj_labels"]["detail"] == "LibsndfileError: boom"
    assert not any("dj_label" in s for s in doc["sections"])
    write_sidecar(tmp_path, doc)
    phrase = merge_own_phrases({"phrases": []}, "s1", tmp_path, source="rbx")["phrases"][0]
    assert phrase["label"] == "inst" and "model_label" not in phrase


def test_energy_pulled_out_after_a_drum_intro_is_a_breakdown_before_the_first_drop():
    plan = [(32.0, ON, -16.0, -12.0, None), (32.0, OFF, OFF, -22.0, None), *CLASSIC[1:]]
    result = label_sections(_sections([p[0] for p in plan]), _envelopes(plan))
    assert _labels(result)[:4] == ["intro", "breakdown", "buildup", "drop"]
    # Control: a pre-drop section at the intro's own level stays intro.
    plan[1] = plan[0]
    result = label_sections(_sections([p[0] for p in plan]), _envelopes(plan))
    assert _labels(result)[:2] == ["intro", "intro"]


def _write_ok_sidecar(tmp_path, stable_id: str, *, lengths: list[float] | None = None) -> dict[str, Any]:
    plan = CLASSIC if lengths is None else [(length, ON, ON, -6.0, None) for length in lengths]
    raw = _raw(_envelopes(plan))
    sections = _sections([p[0] for p in plan] if lengths is None else lengths)
    doc = build_sidecar(stable_id, raw, GRID, GRID_ID)
    write_sidecar(tmp_path, doc)
    return doc


def test_niche_suggestions_list_structure_labels_but_query_stays_empty_until_accepted():
    """[if] dj_labels ok [then] suggestions list sections and query stays empty until accept, else stop."""
    doc = build_sidecar("src", _raw(_envelopes(CLASSIC)), GRID, GRID_ID)
    suggestions = dj_labels.structure_niche_suggestions(doc["sections"], doc["dj_labels"])
    assert suggestions and all(row["source"] == "structure" for row in suggestions)
    state = pipeline.empty_niche_state()
    assert pipeline.accepted_query_labels(state, suggestions) == []


def test_user_niche_region_is_stored_and_listed_back(tmp_path):
    """[if] user records a region [then] it is stored on the track and listed back, else stop."""
    from apps.shared.state import db as state_db
    from apps.shared.state.sync_stamp import canonical_now
    from apps.shared.state.writer import StateWriter

    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        with StateWriter(conn, actor="test") as writer:
            writer.upsert_track(
                stable_id="t1",
                stable_id_tier="inferred",
                title="T",
                artists=["A"],
                album=None,
                isrc=None,
                duration_ms=120_000,
                file_path=None,
            )
            state = pipeline.record_user_region(
                pipeline.empty_niche_state(), start_s=12.0, end_s=44.0, label="weird riser"
            )
            writer.set_field(
                "t1",
                pipeline.NICHE_LABELS_FIELD,
                state,
                source="webui",
                modified_at=canonical_now(),
            )
        listed = pipeline.read_niche_state(conn, "t1")["regions"]
    finally:
        conn.close()
    assert listed == [
        {
            "id": "user:12.0:44.0:weird riser",
            "start_s": 12.0,
            "end_s": 44.0,
            "label": "weird riser",
            "source": "user",
        }
    ]


def test_niche_search_returns_labelled_matches_with_citations_not_cosine_neighbours(tmp_path):
    """[if] one accepted label [then] hits share that label with cited regions, else stop."""
    from apps.shared.state import db as state_db
    from apps.shared.state.sync_stamp import canonical_now
    from apps.shared.state.writer import StateWriter

    data_dir = tmp_path / "data"
    _write_ok_sidecar(data_dir, "src")
    _write_ok_sidecar(data_dir, "hit")
    flat = [(32.0, ON, ON, -6.0, None)] * 6
    flat_doc = build_sidecar("miss", _raw(_envelopes(flat)), GRID, GRID_ID)
    write_sidecar(data_dir, flat_doc)

    db_path = data_dir / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        with StateWriter(conn, actor="test") as writer:
            for sid in ("src", "hit", "miss"):
                writer.upsert_track(
                    stable_id=sid,
                    stable_id_tier="inferred",
                    title=sid,
                    artists=["A"],
                    album=None,
                    isrc=None,
                    duration_ms=120_000,
                    file_path=None,
                )
            suggestions = pipeline.niche_suggestions_for_track(data_dir, "src")
            state = pipeline.set_suggestion_verdict(
                pipeline.empty_niche_state(),
                suggestions[2]["suggestion_id"],
                accepted=True,
            )
            writer.set_field(
                "src",
                pipeline.NICHE_LABELS_FIELD,
                state,
                source="webui",
                modified_at=canonical_now(),
            )
        doc = pipeline.niche_find_similar(conn, data_dir, stable_id="src", limit=10)
        hits = {row["stable_id"] for row in doc["candidates"]}
    finally:
        conn.close()
    assert "hit" in hits and "miss" not in hits
    cited = doc["candidates"][0]["citations"][0]
    assert cited["label"] == "drop" and cited["source"] == "structure"


def test_unlabeled_structure_has_no_suggestions_but_user_regions_still_query(tmp_path):
    """[if] dj_labels unlabeled [then] suggestions empty and user regions still query, else stop."""
    from apps.shared.state import db as state_db
    from apps.shared.state.sync_stamp import canonical_now
    from apps.shared.state.writer import StateWriter

    data_dir = tmp_path / "data"
    flat = [(32.0, ON, ON, -6.0, None)] * 6
    doc = build_sidecar("flat", _raw(_envelopes(flat)), GRID, GRID_ID)
    assert doc["dj_labels"]["status"] == "unlabeled"
    write_sidecar(data_dir, doc)
    db_path = data_dir / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        with StateWriter(conn, actor="test") as writer:
            writer.upsert_track(
                stable_id="flat",
                stable_id_tier="inferred",
                title="flat",
                artists=["A"],
                album=None,
                isrc=None,
                duration_ms=120_000,
                file_path=None,
            )
            state = pipeline.record_user_region(
                pipeline.empty_niche_state(), start_s=1.0, end_s=9.0, label="custom"
            )
            writer.set_field(
                "flat",
                pipeline.NICHE_LABELS_FIELD,
                state,
                source="webui",
                modified_at=canonical_now(),
            )
        suggestions = pipeline.niche_suggestions_for_track(data_dir, "flat")
        labels = pipeline.accepted_query_labels(pipeline.read_niche_state(conn, "flat"), suggestions)
    finally:
        conn.close()
    assert suggestions == [] and labels == ["custom"]


def test_a_silent_lead_in_and_fade_tail_do_not_make_a_flat_track_contrasty():
    """Real-track finding: the quantizer keeps a 0.2 s lead-in and a fade tail as sections."""
    plan = [(0.2, SILENT, SILENT, SILENT, None), *[(32.0, ON, ON, -6.0, None)] * 6]
    plan.append((6.0, SILENT, SILENT, SILENT, None))
    result = label_sections(_sections([p[0] for p in plan]), _envelopes(plan))
    assert result["status"] == "unlabeled" and result["reason"] == "no_energy_contrast"
    # Control: the same track with a real 16 s breakdown is labeled.
    plan.insert(4, (16.0, OFF, OFF, -20.0, None))
    result = label_sections(_sections([p[0] for p in plan]), _envelopes(plan))
    assert result["status"] == "ok" and "breakdown" in _labels(result)
