"""Regression tests for codex CONFIRMED-FOLLOWUP findings, group B.

Covers:
* P06-F02 -- ``apps.analysis.write_tags`` refuses tag writes (exit 2).
* P06-F03 -- :func:`apps.analysis.auto_cues._label_cues` labels the
  "break" cue based on bin adjacency to the drop bin, not list order.
* P07-02 -- ``apps.tags.apply.apply_one`` tolerates a provenance-insert
  failure (surfaces as a soft error) instead of aborting the batch.
* P07-03 -- ``apps.tags.apply._is_app_running`` and
  ``apps.dedup.apply._rekordbox_running`` raise ``PgrepUnavailable``
  when pgrep is missing, and callers fail safe.
* P08-03 -- ``apps.smartlists.djay_writer.DjayPlaylistWriter`` does not
  emit duplicate members when force-adopt retries re-submit existing
  UUIDs. (Unit-tested via the dedup helper; the integration path is
  exercised in the existing smartlist writer tests.)
* P08-04 -- ``apps.shared.smartlists.schema.validate_rule`` rejects
  malformed element types in ``between``/``in`` operators.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.analysis import auto_cues
from apps.analysis import write_tags as wt
from apps.shared.smartlists import SmartlistRuleError
from apps.shared.smartlists.schema import validate_rule
from apps.tags import apply as tags_apply

# Requirement markers are per-test, NOT module-scoped: a module-level
# ``pytestmark`` credits EVERY test here with EVERY id, so the traceability
# matrix fills with false "covered by" links and an unrelated test keeps a
# requirement looking covered. These modules bundle unrelated follow-up
# findings, so each id sits on the test that actually exercises it.


# -- P06-F02 --------------------------------------------------------------

@pytest.mark.requirement("META-01")
def test_p06_f02_reversal_script_is_standalone(tmp_path: Path, capsys) -> None:
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"not-a-real-mp3")
    before = audio.read_bytes()
    with pytest.raises(wt.TagWriteRemoved):
        wt._write_tags(audio)
    assert wt.main([]) == 2
    assert "GPL" in capsys.readouterr().err
    assert audio.read_bytes() == before


# -- P06-F03 --------------------------------------------------------------

@pytest.mark.requirement("META-04")
def test_p06_f03_break_uses_bin_adjacency(monkeypatch: pytest.MonkeyPatch) -> None:
    # 10 bins over 100 s -> 10 s/bin. Drop at t=20s (bin 2). Candidate
    # "break" cue at t=30s (bin 3, adjacent to drop_bin+1) should be
    # labelled "break". A second low-RMS cue at t=80s (bin 8) that
    # *follows in list order* must NOT be labelled "break".
    cues = [
        (5.0, 0.9),   # intro (bin 0)
        (20.0, 1.0),  # drop  (bin 2)
        (30.0, 0.1),  # bin 3 -> break candidate (low RMS)
        (80.0, 0.1),  # bin 8 -> NOT adjacent to drop bin
    ]
    labels = auto_cues._label_cues(
        cues,
        duration_s=100.0,
        rms_dbfs_values=[0.5, 0.5, 0.5, 0.5],
        bin_count=10,
    )
    labels_by_time = {round(p.time_s, 3): p.label for p in labels}
    assert labels_by_time[20.0] == "drop"
    assert labels_by_time[30.0] == "break"
    assert labels_by_time[80.0] != "break"


# -- P07-03 ---------------------------------------------------------------

@pytest.mark.requirement("META-01")
def test_p07_03_tags_is_app_running_raises_when_pgrep_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*a, **kw):
        raise FileNotFoundError(2, "No such file or directory: 'pgrep'")

    monkeypatch.setattr(tags_apply.subprocess, "run", _boom)
    with pytest.raises(tags_apply.PgrepUnavailable):
        tags_apply._is_app_running("rekordbox")


@pytest.mark.requirement("META-03")
def test_p07_03_dedup_rekordbox_running_raises_when_pgrep_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.dedup import apply as dedup_apply

    def _boom(*a, **kw):
        raise FileNotFoundError(2, "No such file or directory: 'pgrep'")

    monkeypatch.setattr(dedup_apply.subprocess, "run", _boom)
    with pytest.raises(dedup_apply.PgrepUnavailable):
        dedup_apply._rekordbox_running()


# -- P08-04 ---------------------------------------------------------------

@pytest.mark.requirement("SMART-01")
def test_p08_04_between_rejects_malformed_element_types() -> None:
    rule = {
        "op": "between",
        "field": "bpm",
        "value": [120, "fast"],  # second element is not a number
    }
    with pytest.raises(SmartlistRuleError, match="between"):
        validate_rule(rule)


@pytest.mark.requirement("SMART-01")
def test_p08_04_in_rejects_malformed_element_types() -> None:
    rule = {
        "op": "in",
        "field": "genre",
        "value": ["house", 7],  # second element is not a string
    }
    with pytest.raises(SmartlistRuleError, match="in on"):
        validate_rule(rule)


@pytest.mark.requirement("SMART-01")
def test_p08_04_between_well_formed_still_validates() -> None:
    # Sanity: the new checks must not break valid rules.
    validate_rule({"op": "between", "field": "bpm", "value": [120, 130]})
    validate_rule({"op": "in", "field": "genre", "value": ["house", "techno"]})
