"""Independent flash checker oracle acceptance tests (issue #14383).

[if] the flash audit miscounts flashes, strobes or phase, or imports the safety limiter [then] fail, [else stop].
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from apps.lighting.fixtures.profile import load_profile
from apps.lighting.flash_audit import RIG, PatchedFixture, RenderedFrame, Strobe, audit
from apps.lighting.universe import SLOT_COUNT

GENERIC = Path("apps/lighting/fixtures/generic")
PAR = load_profile(GENERIC / "generic-par-7.json")
FPS = 40

pytestmark = [pytest.mark.requirement("LIGHT-14426")]


def _par(name: str, address: int) -> PatchedFixture:
    return PatchedFixture(name, PAR, "7ch", 1, address)


def _frame(t: float, pars: dict[int, tuple[int, int, int, int, int]], strobes=None) -> RenderedFrame:
    data = bytearray(SLOT_COUNT)
    for address, values in pars.items():
        data[address - 1 : address - 1 + len(values)] = bytes(values)
    return RenderedFrame(t, {1: bytes(data)}, strobes or {})


def _flashing(rgb: tuple[int, int, int], flashes: int, seconds: float = 1.0) -> list[RenderedFrame]:
    """One par at address 1 flashes ``flashes`` times evenly in ``seconds``, frames at FPS."""
    frames = [_frame(-1 / FPS, {1: (0, *rgb, 0)})]
    total = int(seconds * FPS)
    period = total / flashes
    for i in range(total + FPS):
        on = i < total and (i % period) < period / 2
        frames.append(_frame(i / FPS, {1: (255 if on else 0, *rgb, 0)}))
    return frames


def test_reports_general_and_saturated_red_flashes_per_fixture_and_rig() -> None:
    """[if] a white par flashes 3 times and a red one 5 times in 1 s [then] general and red counts per fixture and rig match."""
    white = audit(_flashing((255, 255, 255), 3), [_par("w", 1)])
    assert white.fixtures["w"].general == 3
    assert white.fixtures["w"].red == 0
    assert white.rig.subject == RIG and white.rig.general == 3
    assert white.ok
    red = audit(_flashing((255, 0, 0), 5), [_par("r", 1)])
    assert red.fixtures["r"].red == 5
    assert red.rig.red == 5
    assert not red.ok


def test_commanded_strobe_hz_counts_as_flashes_per_second() -> None:
    """[if] a steady lit par carries a commanded 6 Hz strobe for 2 s [then] its worst window counts 6 flashes."""
    frames = [_frame(i / FPS, {1: (255, 255, 255, 255, 0)}, {"p": Strobe(6.0)}) for i in range(2 * FPS)]
    report = audit(frames, [_par("p", 1)])
    assert report.fixtures["p"].strobe == 6
    assert report.fixtures["p"].total == 6
    assert report.offenders == ("p",)


def test_flags_out_of_phase_strobing_across_fixtures() -> None:
    """[if] two pars strobe at different phases [then] out-of-phase is flagged, and in phase it is not."""
    pars = {1: (255, 255, 255, 255, 0), 8: (255, 255, 255, 255, 0)}
    apart = [_frame(i / FPS, pars, {"a": Strobe(2.0, 0.0), "b": Strobe(2.0, 0.5)}) for i in range(FPS)]
    together = [_frame(i / FPS, pars, {"a": Strobe(2.0), "b": Strobe(2.0)}) for i in range(FPS)]
    patch = [_par("a", 1), _par("b", 8)]
    assert audit(apart, patch).out_of_phase_at
    assert not audit(apart, patch).ok
    assert audit(together, patch).out_of_phase_at == ()


def test_planted_four_flash_sequence_is_caught_and_fixture_named() -> None:
    """[if] one par of three gets a planted 4-flash second [then] the report fails and names only that par."""
    patch = [_par("left", 1), _par("centre", 8), _par("right", 15)]
    frames = []
    for i in range(3 * FPS):
        t = i / FPS
        burst = FPS <= i < 2 * FPS and (i - FPS) % 10 < 5
        frames.append(
            _frame(t, {1: (128, 0, 0, 255, 0), 8: (255 if burst else 0, 255, 255, 255, 0), 15: (128, 0, 0, 255, 0)})
        )
    report = audit(frames, patch)
    assert report.fixtures["centre"].general == 4
    assert report.offenders == ("centre",)
    assert not report.ok


def test_module_does_not_import_safety() -> None:
    """[if] flash_audit imports apps.lighting.safety [then] fail, as the oracle must stay independent, [else stop]."""
    tree = ast.parse(Path("apps/lighting/flash_audit.py").read_text(encoding="utf-8"))
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any("safety" in name for name in names)
