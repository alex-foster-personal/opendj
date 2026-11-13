"""LIGHT-13789 lighting feed trace record and replay (issue #9865).

[if] a trace stores anything but engine lines parse_state_line reads back [then] fail, [else stop].
"""

from __future__ import annotations

import io
import itertools
import json
import time
from pathlib import Path
from typing import Any

import pytest

from apps.lighting.beat_clock import DeckSample
from apps.lighting.engine_feed import EngineFeedError, MixerSample, parse_state_line
from apps.lighting.trace import TraceReader, TraceWriter

pytestmark = [pytest.mark.requirement("LIGHT-13789"), pytest.mark.requirement("LIGHT-14826")]

ENGINE_LINES = Path(__file__).parent / "fixtures" / "engine_state_lines.ndjson"
FRAME_NS = 33_333_333


def _engine_lines() -> list[dict[str, Any]]:
    """The recorded real engine ``state`` lines the feed adapter tests read."""
    return [json.loads(s) for s in ENGINE_LINES.read_text(encoding="utf-8").splitlines() if s.strip()]


def _record(path: Path, gap_ns: int = FRAME_NS) -> list[dict[str, Any]]:
    """Record every real engine line into ``path`` at ``gap_ns`` spacing; return the lines."""
    lines = _engine_lines()
    with path.open("w", encoding="utf-8") as fh:
        writer = TraceWriter(fh)
        for i, line in enumerate(lines):
            writer.write(line, i * gap_ns)
    return lines


def test_replayed_lines_parse_into_deck_and_mixer_samples(tmp_path: Path) -> None:
    """[if] a replayed entry does not parse into the same DeckSample and MixerSample [then] fail, [else stop]."""
    path = tmp_path / "t.ndjson"
    lines = _record(path)
    entries = list(TraceReader(path).replay(speed=0))
    assert [e.line for e in entries] == lines
    for i, entry in enumerate(entries):
        batch = parse_state_line(entry.line, entry.received_ns, {}, {})
        assert batch == parse_state_line(lines[i], i * FRAME_NS, {}, {})
        assert isinstance(batch.mixer, MixerSample)
        assert batch.decks
        for frame in batch.decks:
            assert isinstance(frame.sample, DeckSample)
            assert frame.sample.host_time_ns == entry.received_ns + entry.line["heard_in_ns"]


def test_writer_refuses_a_line_the_engine_feed_cannot_parse() -> None:
    """[if] the writer stores an invented sample shape [then] fail, [else stop]."""
    writer = TraceWriter(io.StringIO())
    with pytest.raises(EngineFeedError):
        writer.write({"kind": "deck", "deck": "A", "beat": 0, "bar": 0, "bpm": 124.0}, 0)
    with pytest.raises(EngineFeedError, match="missing"):
        writer.write({"type": "state", "state_seq": 0}, 0)


def test_round_trip_byte_identical(tmp_path: Path) -> None:
    """[if] a trace is written and read back [then] every line round-trips byte-identical in JSON form, [else stop]."""
    path = tmp_path / "t.ndjson"
    _record(path)
    texts = path.read_text(encoding="utf-8").splitlines()
    entries = TraceReader(path).read()
    assert len(entries) == len(texts)
    for text, entry in zip(texts, entries, strict=True):
        assert (
            json.dumps({"received_ns": entry.received_ns, "line": entry.line}, sort_keys=True, separators=(",", ":"))
            == text
        )


def test_writer_refuses_time_going_backwards() -> None:
    """[if] received_ns goes backwards [then] the writer raises, [else stop]."""
    line = _engine_lines()[0]
    writer = TraceWriter(io.StringIO())
    writer.write(line, 10)
    with pytest.raises(ValueError, match="backwards"):
        writer.write(line, 9)


def test_speed_two_halves_gaps(tmp_path: Path) -> None:
    """[if] replayed at speed 2 [then] the gap between lines is half the recorded gap within 2 ms, [else stop]."""
    path = tmp_path / "t.ndjson"
    gap_ns = 40_000_000
    _record(path, gap_ns=gap_ns)
    now = [10_000_000_000]

    def sleep(sec: float) -> None:
        now[0] += round(sec * 1e9)

    stamps = [now[0] for _ in TraceReader(path, sleep=sleep, clock=lambda: now[0]).replay(speed=2)]
    assert len(stamps) == len(_engine_lines())
    for a, b in itertools.pairwise(stamps):
        assert abs((b - a) - gap_ns / 2) <= 2_000_000


def test_speed_zero_no_sleep(tmp_path: Path) -> None:
    """[if] replayed at speed 0 [then] no sleep happens and the whole trace replays in under 1 s, [else stop]."""
    path = tmp_path / "t.ndjson"
    lines = _record(path)
    calls: list[float] = []
    start = time.monotonic()
    n = sum(1 for _ in TraceReader(path, sleep=calls.append).replay(speed=0))
    assert time.monotonic() - start < 1.0
    assert calls == []
    assert n == len(lines)


def test_bad_json_names_line(tmp_path: Path) -> None:
    """[if] a line is not valid JSON [then] the reader raises with the line number, [else stop]."""
    path = tmp_path / "bad.ndjson"
    path.write_text('{"received_ns":0,"line":{}}\n{"received_ns":1,"line":{}}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 3"):
        TraceReader(path).read()
