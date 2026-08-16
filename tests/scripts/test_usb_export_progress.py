"""Throughput/ETA maths for the USB export monitor (D1, commit 846dc36d).

The monitor watches a stick grow because rekordbox exposes no progress API.
Everything below runs against tmp directories and hand-built sample windows
-- no real volume is touched, which is the house requirement for USB code
paths (M23), and the monitor is read-only toward a mount in any case.

Regression lines:
  - if the scan stops recursing then a nested Contents/ tree reports as empty
    and the bar never moves
  - if the scan counts macOS index sidecars then the byte total drifts from
    what rekordbox actually wrote
  - if throughput is measured since start rather than across the window then
    a stall is averaged away and the ETA lies
  - if a shrinking or flat window returns a negative or non-zero rate then
    the ETA reads as progress during a stall
  - if a stalled run does not surface as "stalled" then a hang looks like a
    slow export
  - if an absurd ETA is rendered verbatim then the panel shows a 900-hour
    estimate instead of admitting it does not know
"""

from __future__ import annotations

import io
from collections import deque

from rich.console import Console

from scripts import usb_export_progress as mon


def _sample(at: float, total_bytes: int, files: int = 1) -> mon.Sample:
    return mon.Sample(at=at, total_bytes=total_bytes, files=files)


# ----- scanning -------------------------------------------------------------


def test_scan_totals_bytes_and_files_recursively(tmp_path) -> None:
    (tmp_path / "Contents" / "Artist").mkdir(parents=True)
    (tmp_path / "Contents" / "Artist" / "a.mp3").write_bytes(b"x" * 100)
    (tmp_path / "Contents" / "b.mp3").write_bytes(b"y" * 50)
    (tmp_path / "top.dat").write_bytes(b"z" * 7)
    sample = mon._scan(tmp_path)
    assert sample.total_bytes == 157
    assert sample.files == 3


def test_scan_ignores_macos_index_sidecars(tmp_path) -> None:
    (tmp_path / "Contents").mkdir()
    (tmp_path / "Contents" / "a.mp3").write_bytes(b"x" * 10)
    for noise in (".Spotlight-V100", ".fseventsd"):
        (tmp_path / noise).mkdir()
        (tmp_path / noise / "junk").write_bytes(b"q" * 999)
    sample = mon._scan(tmp_path)
    assert sample.total_bytes == 10
    assert sample.files == 1


def test_scan_of_an_empty_tree_is_zero_not_an_error(tmp_path) -> None:
    sample = mon._scan(tmp_path)
    assert (sample.total_bytes, sample.files) == (0, 0)


# ----- rolling throughput ---------------------------------------------------


def test_rate_needs_two_samples() -> None:
    assert mon._rate(deque([_sample(0.0, 100)])) == 0.0
    assert mon._rate(deque()) == 0.0


def test_rate_is_bytes_per_second_across_the_window() -> None:
    window = deque([_sample(0.0, 0), _sample(10.0, 5_000)])
    assert mon._rate(window) == 500.0


def test_a_flat_window_reads_zero_even_after_earlier_growth() -> None:
    """The whole point of the rolling window: a stall must not be averaged
    away by throughput from the start of the run."""
    window: deque = deque(maxlen=mon.WINDOW)
    for i in range(mon.WINDOW):
        window.append(_sample(float(i * 10), 1_000_000))
    assert mon._rate(window) == 0.0


def test_growth_leaves_the_window_once_it_is_full() -> None:
    window: deque = deque(maxlen=mon.WINDOW)
    window.append(_sample(0.0, 0))
    for i in range(1, mon.WINDOW + 1):
        window.append(_sample(float(i * 10), 1_000_000))
    assert len(window) == mon.WINDOW
    assert mon._rate(window) == 0.0


def test_the_window_is_short_enough_that_a_stall_surfaces_quickly() -> None:
    """R2 is "rolling window, not since-start", and a window wide enough to
    span the whole export is since-start wearing a deque. At the default 10s
    sampling interval the window must clear inside a couple of minutes, or a
    stall is still being averaged away -- just more slowly."""
    default_interval_s = 10
    assert 2 <= mon.WINDOW <= 12
    assert mon.WINDOW * default_interval_s <= 120


def test_a_shrinking_total_never_reports_a_negative_rate() -> None:
    window = deque([_sample(0.0, 5_000), _sample(10.0, 1_000)])
    assert mon._rate(window) == 0.0


def test_a_zero_length_window_does_not_divide_by_zero() -> None:
    window = deque([_sample(5.0, 0), _sample(5.0, 900)])
    assert mon._rate(window) == 0.0


# ----- ETA rendering --------------------------------------------------------


def test_no_rate_renders_as_stalled() -> None:
    assert mon._fmt_eta(None) == "stalled"


def test_an_absurd_eta_admits_it_is_unknown() -> None:
    assert mon._fmt_eta(86_401) == "unknown"
    assert mon._fmt_eta(-1) == "unknown"


def test_eta_under_an_hour_reads_minutes_and_seconds() -> None:
    assert mon._fmt_eta(125) == "2m 05s"


def test_eta_over_an_hour_reads_hours_and_minutes() -> None:
    assert mon._fmt_eta(3 * 3600 + 7 * 60) == "3h 07m"


def test_byte_formatting_climbs_the_units() -> None:
    assert mon._fmt_bytes(512) == "512.0 B"
    assert mon._fmt_bytes(1536) == "1.5 KB"
    assert mon._fmt_bytes(5 * 1024**3) == "5.0 GB"
    assert mon._fmt_bytes(2 * 1024**4) == "2.0 TB"


def _render(renderable) -> str:
    console = Console(width=100, record=True, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def test_panel_reports_written_files_rate_eta_and_elapsed() -> None:
    rendered = _render(
        mon._panel(
            start_bytes=1_000, cur=_sample(30.0, 3_000, files=12),
            rate=100.0, expect=10_000, t0=0.0,
        )
    )
    for label in ("written", "files", "rate", "ETA", "elapsed"):
        assert label in rendered, rendered
    assert "12" in rendered            # file count
    assert "2.0 KB" in rendered        # 3000 - 1000 written
    assert "1m 20s" in rendered        # (10000 - 2000) remaining at 100 B/s


def test_panel_calls_a_zero_rate_stalled_rather_than_showing_an_eta() -> None:
    rendered = _render(
        mon._panel(start_bytes=0, cur=_sample(30.0, 500), rate=0.0, expect=10_000, t0=0.0)
    )
    assert "stalled" in rendered
