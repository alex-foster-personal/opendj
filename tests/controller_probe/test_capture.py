"""Unit tests for the live MIDI capture probe's pure logic.

The probe is a standalone PEP 723 script, not a package module, so it is loaded
by path. That is deliberate: it has to keep running under a bare
``uv run tools/controller-probe/capture.py`` with no repo install, which rules
out importing it as ``tools.controller_probe.capture``.

The decode/lookup/verdict/diff functions under test are the whole adjudication
contract. The CoreMIDI transport around them is exercised against the real
device by hand (see tools/controller-probe/README.md); nothing here mocks it,
and nothing here would pass if the transport were broken in a way these
functions could see.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = REPO_ROOT / "tools" / "controller-probe" / "capture.py"
MAP_PATH = REPO_ROOT / "tools" / "controller-probe" / "flx4_expected_map.json"


def _load_probe() -> Any:
    spec = importlib.util.spec_from_file_location("controller_probe_capture", PROBE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load probe module from {PROBE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], and a by-path load that skips this step dies
    # inside dataclasses with an unrelated-looking AttributeError.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


# --------------------------------------------------------------------- decode


@pytest.mark.requirement("CTRL-02")
def test_decode_note_on() -> None:
    """[if] a note-on decodes to another family or channel [then] verdicts lie, [else stop]."""
    decoded = probe.decode_message([0x90, 11, 127])
    assert decoded is not None
    assert (decoded.kind, decoded.channel, decoded.code, decoded.value) == ("note_on", 1, 11, 127)
    assert decoded.status == 0x90


@pytest.mark.requirement("CTRL-02")
def test_decode_note_off_and_zero_velocity_release() -> None:
    """[if] a zero-velocity note-on is not a release [then] note steps hang, [else stop]."""
    note_off = probe.decode_message([0x81, 11, 0])
    assert note_off is not None
    assert (note_off.kind, note_off.channel) == ("note_off", 2)
    assert probe.is_note_release(note_off)

    zero_velocity = probe.decode_message([0x90, 11, 0])
    assert zero_velocity is not None
    assert zero_velocity.kind == "note_on"
    assert probe.is_note_release(zero_velocity)

    press = probe.decode_message([0x90, 11, 127])
    assert press is not None
    assert not probe.is_note_release(press)


@pytest.mark.requirement("CTRL-02")
def test_decode_control_change() -> None:
    """[if] a CC decodes with the wrong code [then] the reverse lookup misses, [else stop]."""
    decoded = probe.decode_message([0xB0, 34, 64])
    assert decoded is not None
    assert (decoded.kind, decoded.channel, decoded.code, decoded.value) == ("cc", 1, 34, 64)


@pytest.mark.requirement("CTRL-02")
def test_decode_14bit_pair_yields_two_distinct_codes() -> None:
    """[if] MSB and LSB collapse to one pair [then] 14-bit wires stay hidden, [else stop]."""
    msb = probe.decode_message([0xB0, 34, 64])
    lsb = probe.decode_message([0xB0, 66, 12])
    assert msb is not None and lsb is not None
    pairs = {(msb.status, msb.code), (lsb.status, lsb.code)}
    assert pairs == {(0xB0, 34), (0xB0, 66)}


@pytest.mark.requirement("CTRL-02")
def test_realtime_status_bytes_are_dropped() -> None:
    """[if] clock decodes as a control [then] the log floods at 24ppqn, [else stop]."""
    for status in (0xF8, 0xFA, 0xFC, 0xFE, 0xFF):
        assert probe.is_realtime(status)
        assert probe.decode_message([status]) is None


@pytest.mark.requirement("CTRL-02")
def test_non_status_and_empty_messages_decode_to_none() -> None:
    """[if] a running-status fragment decodes [then] bogus pairs enter captures, [else stop]."""
    assert probe.decode_message([]) is None
    assert probe.decode_message([0x40, 0x01]) is None
    assert probe.decode_message([0x90]) is None


# --------------------------------------------------------------- reverse lookup


@pytest.mark.requirement("CTRL-02")
def test_reverse_lookup_hit() -> None:
    """[if] a known pair does not resolve [then] every sniff line reads UNMAPPED, [else stop]."""
    expected_map = probe.load_expected_map(MAP_PATH)
    control = probe.lookup_control(expected_map.index, 144, 11)
    assert control is not None
    assert control.id == "deck1_play_pause"


@pytest.mark.requirement("CTRL-02")
def test_reverse_lookup_miss_reports_absent() -> None:
    """[if] an absent pair resolves to a neighbor [then] mismatches read as matches, [else stop]."""
    expected_map = probe.load_expected_map(MAP_PATH)
    # Negative control: the map holds note-ON 0x90 code 11 and no CC at code 11
    # on channel 16, so both of these MUST report absent. If either resolves,
    # the index is matching on something looser than the exact pair.
    assert probe.lookup_control(expected_map.index, 0xBF, 11) is None
    assert probe.lookup_control(expected_map.index, 0x90, 126) is None


@pytest.mark.requirement("CTRL-02")
def test_note_release_is_accounted_for_but_unknown_cc_is_not() -> None:
    """[if] releases count as EXTRA [then] real unknown wires drown in noise, [else stop]."""
    expected_map = probe.load_expected_map(MAP_PATH)
    assert probe.is_accounted_for(expected_map.index, 0x80, 11) is True
    assert probe.is_accounted_for(expected_map.index, 0xB0, 66) is False


# -------------------------------------------------------------------- verdicts


def _expected(status: int = 144, code: int = 11) -> dict[str, Any]:
    return {"status": status, "ch": 1, "code": code, "kind": "note"}


def _observed(status: int, code: int) -> dict[str, Any]:
    return {
        "t_utc": "2026-09-18T00:00:00.000+00:00",
        "bytes": [status, code, 127],
        "status": status,
        "ch": 1,
        "code": code,
        "value": 127,
    }


@pytest.mark.requirement("CTRL-02")
def test_verdict_match_needs_the_exact_pair() -> None:
    """[if] a near-miss scores match [then] the probe rubber-stamps the theory map, [else stop]."""
    assert probe.compute_verdict(_expected(), [_observed(144, 11)], False) == "match"
    assert (
        probe.compute_verdict(_expected(), [_observed(144, 12), _observed(144, 11)], False)
        == "match"
    )


@pytest.mark.requirement("CTRL-02")
def test_verdict_mismatch_on_one_wrong_byte() -> None:
    """[if] a wrong status or code scores match [then] diff can never go red, [else stop]."""
    assert probe.compute_verdict(_expected(), [_observed(145, 11)], False) == "mismatch"
    assert probe.compute_verdict(_expected(), [_observed(144, 12)], False) == "mismatch"


@pytest.mark.requirement("CTRL-02")
def test_verdict_skipped_when_flagged_or_silent() -> None:
    """[if] silence scores mismatch [then] an untouched control indicts the map, [else stop]."""
    assert probe.compute_verdict(_expected(), [_observed(144, 11)], True) == "skipped"
    assert probe.compute_verdict(_expected(), [], False) == "skipped"


# --------------------------------------------------------------------- filters


@pytest.mark.requirement("CTRL-02")
def test_filter_controls_narrows_and_keeps_file_order() -> None:
    """[if] filters reorder or ignore a flag [then] the guided walk misleads, [else stop]."""
    expected_map = probe.load_expected_map(MAP_PATH)
    deck1 = probe.filter_controls(expected_map.controls, section="deck", deck=1)
    assert deck1
    assert all(c.section == "deck" and c.deck == 1 for c in deck1)
    order = [c.id for c in expected_map.controls if c.section == "deck" and c.deck == 1]
    assert [c.id for c in deck1] == order

    unshifted = probe.filter_controls(expected_map.controls, section="deck", no_shift=True)
    assert unshifted
    assert not any(c.shift for c in unshifted)

    picked = probe.filter_controls(expected_map.controls, ids=["deck1_cue", "deck1_play_pause"])
    assert [c.id for c in picked] == ["deck1_play_pause", "deck1_cue"]


@pytest.mark.requirement("CTRL-02")
def test_filter_controls_rejects_unknown_id() -> None:
    """[if] a typo'd id silently selects nothing [then] a walk captures nothing, [else stop]."""
    expected_map = probe.load_expected_map(MAP_PATH)
    with pytest.raises(SystemExit):
        probe.filter_controls(expected_map.controls, ids=["deck1_play_pause", "no_such_control"])


# ----------------------------------------------------------------- port picker


@pytest.mark.requirement("CTRL-02")
def test_resolve_port_requires_exactly_one_match() -> None:
    """[if] an ambiguous needle picks the first port [then] the probe reads a guess, [else stop]."""
    assert probe.resolve_port(["IAC Driver Bus 1", "DDJ-FLX4"], "ddj-flx4") == 1
    with pytest.raises(SystemExit):
        probe.resolve_port(["IAC Driver Bus 1"], "DDJ-FLX4")
    with pytest.raises(SystemExit):
        probe.resolve_port(["DDJ-FLX4", "DDJ-FLX4 Port 2"], "DDJ-FLX4")


# ------------------------------------------------------------------ diff gate


def _capture_doc(observed_status: int) -> dict[str, Any]:
    """A hand-written capture: one note control, one CC control with a stray LSB."""
    return {
        "device": "DDJ-FLX4",
        "port": "DDJ-FLX4",
        "map_path": str(MAP_PATH),
        "map_sha256": "0" * 64,
        "started_utc": "2026-09-18T00:00:00.000+00:00",
        "entries": [
            {
                "id": "deck1_play_pause",
                "expected": {"status": 144, "ch": 1, "code": 11, "kind": "note"},
                "observed": [_observed(observed_status, 11), _observed(128, 11)],
                "verdict": "match",
            },
            {
                "id": "deck1_jog_vinyl_on",
                "expected": {"status": 176, "ch": 1, "code": 34, "kind": "cc"},
                "observed": [_observed(176, 34), _observed(176, 66)],
                "verdict": "match",
            },
        ],
    }


def _write_capture(tmp_path: Path, observed_status: int) -> Path:
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(_capture_doc(observed_status)), encoding="utf-8")
    return path


@pytest.mark.requirement("CTRL-02")
def test_diff_exits_1_on_one_mismatched_byte(tmp_path: Path) -> None:
    """[if] a one-byte-wrong capture exits 0 [then] the gate cannot fail, [else stop]."""
    path = _write_capture(tmp_path, observed_status=145)
    assert probe.main(["diff", str(path), "--map", str(MAP_PATH)]) == 1


@pytest.mark.requirement("CTRL-02")
def test_diff_exits_0_when_the_byte_is_corrected(tmp_path: Path) -> None:
    """[if] a correct capture exits 1 [then] the gate blocks every adjudication, [else stop]."""
    path = _write_capture(tmp_path, observed_status=144)
    assert probe.main(["diff", str(path), "--map", str(MAP_PATH)]) == 0


@pytest.mark.requirement("CTRL-02")
def test_diff_report_counts_and_lists_extras() -> None:
    """[if] an unknown pair is dropped [then] a real wire discovery is lost, [else stop]."""
    expected_map = probe.load_expected_map(MAP_PATH)
    report = probe.build_diff_report(_capture_doc(145), expected_map)
    assert (report.match, report.mismatch, report.skipped) == (1, 1, 0)
    assert report.not_attempted == len(expected_map.controls) - 2
    # 0xB0/66 is the 14-bit LSB companion: unknown to the map, so it must be
    # reported. 0x80/11 is a release of a mapped note, so it must not be.
    assert (0xB0, 66) in report.extras
    assert (0x80, 11) not in report.extras
    assert "match 1 / mismatch 1 / skipped 0 / not-attempted" in report.summary_line
