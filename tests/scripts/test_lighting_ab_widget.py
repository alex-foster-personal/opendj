"""Tests for scripts/lighting_ab_widget.py (LIGHT-10561, issue #9905).

[if] the widget loads external URLs or leaks variant ids [then] fail, [else stop].
[if] the page lacks click-to-seek or two stage canvases [then] fail, [else stop].
[if] the same seed yields a different pair order [then] fail, [else stop].
[if] malformed frames or audio are accepted [then] fail, [else stop].
"""

from __future__ import annotations

import base64
import json
import math
import re
import struct
import wave
from html.parser import HTMLParser
from pathlib import Path

import pytest

from scripts.lighting_ab_widget import build_pairs, main, render_widget

pytestmark = pytest.mark.requirement("LIGHT-10561")

IDS = ("zzcoolvariant", "zzwarmvariant")


# ----- fixtures ----------------------------------------------------------------------------


def _write_sine_wav(path: Path, seconds: float = 10.0, rate: int = 22050) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        samples = (int(12000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(seconds * rate)))
        handle.writeframes(b"".join(struct.pack("<h", s) for s in samples))
    return path


def _frames(colour: list[int], fps: int = 30, seconds: int = 10) -> dict:
    start = 60_000_000_000
    return {
        "fps": fps,
        "frames": [
            {
                "t_ns": start + i * 1_000_000_000 // fps,
                "fixtures": [
                    {"id": f"par{k}", "x": 0.2 + 0.3 * k, "y": 0.5, "color": colour, "intensity": (i % fps) / fps}
                    for k in range(3)
                ],
            }
            for i in range(fps * seconds)
        ],
    }


def _write_frames(path: Path, doc: object) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, list[tuple[str, Path]]]:
    audio = _write_sine_wav(tmp_path / "excerpt.wav")
    cool = _write_frames(tmp_path / "cool.frames.json", _frames([0, 80, 255]))
    warm = _write_frames(tmp_path / "warm.frames.json", _frames([255, 90, 0]))
    return audio, [(IDS[0], cool), (IDS[1], warm)]


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._in_script = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.scripts.append(data)


def _parse(text: str) -> _Collector:
    collector = _Collector()
    collector.feed(text)
    return collector


# ----- acceptance --------------------------------------------------------------------------


def test_generator_writes_one_offline_html_file(tmp_path: Path, inputs) -> None:
    audio, variants = inputs
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    args = ["--audio", str(audio), "--pairs", "6", "--seed", "1", "--out", str(out_dir / "ab.html")]
    for variant_id, path in variants:
        args += ["--variant", f"{variant_id}={path}"]
    assert main(args) == 0
    assert [p.name for p in out_dir.iterdir()] == ["ab.html"]
    text = (out_dir / "ab.html").read_text(encoding="utf-8")
    for banned in ("http://", "https://", "//cdn", "<link", "fetch(", "XMLHttpRequest", "@import"):
        assert banned not in text, banned
    for _, attrs in _parse(text).tags:
        for key in ("src", "href"):
            value = attrs.get(key)
            assert value is None or value.startswith(("data:", "blob:")), value
    assert "default-src 'none'" in text


def test_html_has_click_to_seek_waveform_and_two_stage_canvases(inputs) -> None:
    audio, variants = inputs
    parsed = _parse(render_widget(audio, variants, 4, 1))
    assert sum(1 for _, a in parsed.tags if a.get("id") == "waveform") == 1
    assert sum(1 for t, a in parsed.tags if t == "canvas" and a.get("class") == "stage") == 2
    assert any(t == "audio" and a.get("id") == "audio" for t, a in parsed.tags)
    script = "".join(parsed.scripts)
    assert 'waveform.addEventListener("click"' in script
    assert "audio.currentTime =" in script


def test_same_seed_gives_identical_pair_order(inputs) -> None:
    audio, variants = inputs
    assert build_pairs(3, 24, seed=7) == build_pairs(3, 24, seed=7)
    assert build_pairs(3, 24, seed=7) != build_pairs(3, 24, seed=8)
    assert render_widget(audio, variants, 8, 3) == render_widget(audio, variants, 8, 3)


def test_pairs_balance_combinations_and_sides() -> None:
    pairs = build_pairs(3, 24, seed=5)
    counts: dict[frozenset[int], int] = {}
    for pair in pairs:
        assert pair.left != pair.right
        key = frozenset((pair.left, pair.right))
        counts[key] = counts.get(key, 0) + 1
    assert sorted(counts.values()) == [8, 8, 8]


def test_variant_ids_are_not_in_page_source(inputs) -> None:
    audio, variants = inputs
    text = render_widget(audio, variants, 4, 1)
    for variant_id in IDS:
        assert variant_id not in text
    match = re.search(r'JSON\.parse\(atob\("([A-Za-z0-9+/=]+)"\)\)', text)
    assert match
    assert json.loads(base64.b64decode(match.group(1)))["variant_ids"] == list(IDS)


# ----- fail fast ---------------------------------------------------------------------------


def _bad_frames(tmp_path: Path, mutate) -> Path:
    doc = _frames([10, 10, 10], seconds=1)
    mutate(doc["frames"])
    return _write_frames(tmp_path / "bad.frames.json", doc)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda f: f[0].pop("t_ns"), r"frames\[0\]\.t_ns must be an int"),
        (lambda f: f[1].update(t_ns=f[0]["t_ns"]), "strictly increasing"),
        (
            lambda f: f[3]["fixtures"][0].update(intensity=1.4),
            r"bad\.frames\.json: frames\[3\]\.fixtures\[0\]\.intensity",
        ),
    ],
)
def test_bad_frames_raise(tmp_path: Path, inputs, mutate, match: str) -> None:
    audio, variants = inputs
    bad = _bad_frames(tmp_path, mutate)
    with pytest.raises(ValueError, match=match):
        render_widget(audio, [variants[0], ("bad", bad)], 2, 1)


def test_bad_variant_counts_and_pairs_raise(inputs) -> None:
    audio, variants = inputs
    path = variants[0][1]
    with pytest.raises(ValueError, match="2 to 4 variants"):
        render_widget(audio, variants[:1], 2, 1)
    with pytest.raises(ValueError, match="2 to 4 variants"):
        render_widget(audio, [(f"v{i}", path) for i in range(5)], 2, 1)
    with pytest.raises(ValueError, match="unique"):
        render_widget(audio, [("same", path), ("same", path)], 2, 1)
    with pytest.raises(ValueError, match="pair count"):
        render_widget(audio, variants, 0, 1)


def test_bad_audio_raises(tmp_path: Path, inputs) -> None:
    _, variants = inputs
    ogg = tmp_path / "x.ogg"
    ogg.write_bytes(b"OggS0000")
    with pytest.raises(ValueError, match=r"\.wav or \.mp3"):
        render_widget(ogg, variants, 2, 1)
    fake = tmp_path / "fake.wav"
    fake.write_bytes(b"not a wav file at all")
    with pytest.raises(ValueError, match="not a valid"):
        render_widget(fake, variants, 2, 1)


def test_main_reports_error_and_exits_2(tmp_path: Path, inputs, capsys) -> None:
    audio, variants = inputs
    args = ["--audio", str(audio), "--pairs", "0", "--seed", "1", "--out", str(tmp_path / "ab.html")]
    for variant_id, path in variants:
        args += ["--variant", f"{variant_id}={path}"]
    assert main(args) == 2
    assert "[ERROR]" in capsys.readouterr().err
