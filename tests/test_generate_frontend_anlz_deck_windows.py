"""The synthetic ANLZ deck windows never carry captured band data (issue #13446).

- if the generator copies detail bands from the fixture it overwrites then broken
- if two runs of the generator write different windows then broken
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import generate_frontend_anlz_deck_windows as gen


def test_windows_ignore_existing_capture_bands(tmp_path: Path) -> None:
    """Writing a window over a file holding other bands keeps none of them."""
    for spec in (gen.BLOCKS, gen.GOTHIC):
        n = spec.point_range[1] - spec.point_range[0]
        target = tmp_path / spec.json_name
        captured = {"low": [0.1234] * n, "mid": [0.4321] * n, "high": [0.2222] * n}
        target.write_text(json.dumps({"detail": captured, "beats": [{"t": 1.0}]}), "utf-8")
        written = json.loads(gen._apply_spec(target, spec))
        for band, values in written["detail"].items():
            assert len(values) == n
            assert values != captured[band], f"{spec.json_name} {band} copied the old capture"
        assert written == {**written, **gen.synthetic_window(spec)}


def test_synthetic_windows_are_deterministic_and_in_window() -> None:
    """Each spec yields the same PWV-quantised bands every run, beats inside its window."""
    for spec in (gen.BLOCKS, gen.GOTHIC):
        first, second = gen.synthetic_window(spec), gen.synthetic_window(spec)
        assert first == second
        for values in first["detail"].values():
            assert all(0.0 <= v <= 1.0 and abs(v * 127 - round(v * 127)) < 0.01 for v in values)
        t0, t1 = spec.window_s
        assert first["beats"] and all(t0 <= b["t"] < t1 for b in first["beats"])


def test_committed_fixtures_match_the_generator() -> None:
    """The committed fixtures are exactly what the synthetic model writes."""
    for spec in (gen.BLOCKS, gen.GOTHIC):
        on_disk = json.loads((gen.FIX / spec.json_name).read_text("utf-8"))
        window = gen.synthetic_window(spec)
        assert on_disk["detail"] == window["detail"], spec.json_name
        assert on_disk["beats"] == window["beats"], spec.json_name
