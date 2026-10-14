"""Installed-app acceptance: a PASS must rest on having seen the good thing (OPS-55).

Two verdict rules that could pass without it: the paused-audio control, which
trusted one early silent reading over a later fresh audible one, and the stem
bundle, which counted part names and streamed whichever part came first.

[if] the paused output is audible, or a canonical stem part is missing or does not stream [then] the check does not pass, [else stop]

Regression lines:
  - if a fresh audible level after pause passes because an earlier reading was silent then broken
  - if a playhead that moves after pause passes because an earlier reading was silent then broken
  - if a silent paused control after audible playback fails then broken
  - if four stem parts with the wrong names count as a real bundle then broken
  - if a bundle whose drums part does not stream counts as real then broken
  - if a bundle with every canonical part streaming is not real then broken
  - if the canonical part names drift from the stems code then broken
  - if lyrics that only contain a fixture word inside a longer word match then broken
  - if the fixture's own spoken line does not match its words then broken
  - if a running app whose Info.plist cannot be read is skipped instead of refusing the run then broken
  - if a running app with a different, readable identifier refuses the run then broken
"""
from __future__ import annotations

import json
import plistlib
from pathlib import Path
from typing import cast

import pytest

from apps.stems.artifacts import STEM_PARTS
from scripts import app_acceptance as acc

pytestmark = pytest.mark.requirement("OPS-55")


def _meter(rms_max: float | None, first: float, last: float, playing_seen: bool) -> dict:
    return {"rms_max": rms_max, "position_first": first, "position_last": last,
            "playing_seen": playing_seen, "fresh_samples": 20}


# Shapes measured on demon-llama, Wed 7 Oct 2026, installed 2411a8f2.
PLAYING = _meter(0.1131, 553.2, 4137.2, True)
PAUSED = _meter(0.0, 5920.0, 5920.0, False)


def test_fresh_audible_level_after_pause_never_passes():
    """if a fresh audible level after pause passes because an earlier reading was silent then broken"""
    frozen_loud = _meter(0.0109, 5920.0, 5920.0, False)
    for silent in (True, False):
        verdict, summary = acc.audio_verdict(True, silent, PLAYING, frozen_loud, device_ok=True)
        assert verdict == acc.UNKNOWN and "0.0109" in summary, (silent, verdict, summary)


def test_playhead_moving_after_pause_never_passes():
    """if a playhead that moves after pause passes because an earlier reading was silent then broken"""
    for paused in (_meter(0.139, 4899.9, 5216.0, False), _meter(0.0, 4899.9, 5216.0, False)):
        for silent in (True, False):
            verdict, summary = acc.audio_verdict(True, silent, PLAYING, paused, device_ok=True)
            assert verdict == acc.FAIL and "pause" in summary, (silent, verdict, summary)


def test_silent_frozen_pause_after_audible_playback_passes():
    """if a silent paused control after audible playback fails then broken"""
    just_under = _meter(acc.RMS_AUDIBLE_MIN / 2, 5920.0, 5920.0, False)
    for paused in (PAUSED, just_under):
        assert acc.audio_verdict(True, True, PLAYING, paused, device_ok=True)[0] == acc.PASS


class BundleEngine:
    """A stems surface: a manifest with the given parts, and a byte size per part that streams."""

    def __init__(self, parts: list[str], stream_bytes: dict[str, int]) -> None:
        self.parts, self.stream_bytes = parts, stream_bytes
        self.streamed: list[str] = []

    def call(self, method: str, path: str, body: object = None, timeout: float = 30) -> tuple[int, dict, dict]:
        if path.endswith("/stems/state"):
            return 200, {"state": "local"}, {}
        return 200, {"parts": {p: f"{p}.flac" for p in self.parts}, "frame_count": 44100 * 30,
                     "sample_rate_hz": 44100, "model": "htdemucs"}, {}

    def get_size(self, path: str, timeout: float = 60) -> tuple[int, int]:
        part = path.rsplit("/", 1)[1]
        self.streamed.append(part)
        return (200, self.stream_bytes[part]) if part in self.stream_bytes else (404, 0)


GOOD = dict.fromkeys(("vocals", "drums", "bass", "other"), 400_000)


def test_canonical_parts_match_the_stems_code():
    """if the canonical part names drift from the stems code then broken"""
    assert acc.STEM_PARTS == STEM_PARTS == ("vocals", "drums", "bass", "other")


def test_bundle_with_every_canonical_part_streaming_is_real():
    """if a bundle with every canonical part streaming is not real then broken"""
    engine = BundleEngine(list(GOOD), GOOD)
    bundle = acc._stem_bundle(engine, "t1", 30.0)
    assert bundle is not None and bundle["real"] is True, bundle
    assert sorted(engine.streamed) == sorted(acc.STEM_PARTS)  # control: every required part was fetched
    assert bundle["missing_parts"] == [] and bundle["parts_not_streaming"] == []


@pytest.mark.parametrize("parts", [
    ["a", "b", "c", "d"],
    ["drums", "bass", "other", "instrumental"],
    ["vocals", "drums", "bass", "bogus"],
    ["vocals_extra", "drums", "bass", "other"],
])
def test_four_parts_with_the_wrong_names_are_not_a_real_bundle(parts: list[str]):
    """if four stem parts with the wrong names count as a real bundle then broken"""
    bundle = acc._stem_bundle(cast(acc.Engine, BundleEngine(parts, dict.fromkeys(parts, 400_000))), "t1", 30.0)
    assert bundle is not None and bundle["real"] is False, bundle
    assert bundle["missing_parts"] == sorted(set(acc.STEM_PARTS) - set(parts))


@pytest.mark.parametrize("broken", [{"drums": 0}, {"other": 9_000}, {"bass": None}, {"vocals": 0}])
def test_a_canonical_part_that_does_not_stream_is_not_a_real_bundle(broken: dict[str, int | None]):
    """if a bundle whose drums part does not stream counts as real then broken"""
    sizes = {**GOOD, **broken}
    part = next(iter(broken))
    bundle = acc._stem_bundle(cast(acc.Engine, BundleEngine(list(GOOD), {k: v for k, v in sizes.items() if v is not None})), "t1", 30.0)
    assert bundle is not None and bundle["real"] is False, bundle
    assert bundle["parts_not_streaming"] == [part], bundle


# ----- lyrics: whole words only ------------------------------------------------------------
FIXTURE_WORDS = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "app-acceptance" / "fixture.json").read_text()
)["lyrics_words"]
# What make_fixture.sh has `say` speak.
SPOKEN = ("Open D J acceptance. The installed app must play this voice. Open D J acceptance. "
          "Every build is tested before it ships.")


def test_a_fixture_word_inside_a_longer_word_does_not_match():
    """if lyrics that only contain a fixture word inside a longer word match then broken"""
    assert FIXTURE_WORDS == ["acceptance", "voice", "build"]  # control: these are the words under test
    unrelated = "Send the invoice to the building, the rebuilding of unacceptances and voiceless invoices"
    assert acc.fixture_words_in(unrelated, FIXTURE_WORDS) == []


@pytest.mark.parametrize("text,expected", [
    (SPOKEN, ["acceptance", "voice", "build"]),
    ("play this VOICE!", ["voice"]),
    ("every\nbuild, tested", ["build"]),
    ("the voice's build-up", ["voice", "build"]),
    ("", []),
])
def test_the_fixtures_own_words_still_match(text: str, expected: list[str]):
    """if the fixture's own spoken line does not match its words then broken"""
    assert acc.fixture_words_in(text, FIXTURE_WORDS) == expected


# ----- single-instance guard: fail closed --------------------------------------------------
OURS = "com.example.opendj"


def _app(root: Path, name: str, plist: bytes | None) -> acc.Proc:
    """A running process for an app bundle under root, with the given Info.plist bytes (None = no file)."""
    contents = root / f"{name}.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    if plist is not None:
        (contents / "Info.plist").write_bytes(plist)
    return acc.Proc(100 + len(list(root.iterdir())), 1, 1024, f"{contents}/MacOS/{name} --flag value")


def test_a_running_app_with_our_identifier_is_reported(tmp_path: Path):
    """if a running instance of the same bundle identifier is not found then broken"""
    ours = _app(tmp_path, "Open DJ", plistlib.dumps({"CFBundleIdentifier": OURS}))
    other = _app(tmp_path, "Mail", plistlib.dumps({"CFBundleIdentifier": "com.example.mail"}))
    plain = acc.Proc(7, 1, 1024, "/usr/sbin/syslogd")
    assert acc.same_identifier_processes([plain, other, ours], OURS) == [f"pid {ours.pid}: {tmp_path / 'Open DJ.app'}"]


def test_other_running_apps_with_readable_identifiers_do_not_refuse(tmp_path: Path):
    """if a running app with a different, readable identifier refuses the run then broken"""
    procs = [
        _app(tmp_path, "Mail", plistlib.dumps({"CFBundleIdentifier": "com.example.mail"})),
        _app(tmp_path, "Binary", plistlib.dumps({"CFBundleIdentifier": "com.example.bin"}, fmt=plistlib.FMT_BINARY)),
        _app(tmp_path, "Nameless", plistlib.dumps({"CFBundleName": "no identifier at all"})),
        acc.Proc(7, 1, 1024, "/usr/sbin/syslogd"),
    ]
    assert acc.same_identifier_processes(procs, OURS) == []


@pytest.mark.parametrize("plist", [None, b"", b"not a plist", plistlib.dumps(["a", "list"])],
                         ids=["missing", "empty", "garbage", "not-a-dict"])
def test_a_running_app_that_cannot_be_identified_refuses_the_run(tmp_path: Path, plist: bytes | None):
    """if a running app whose Info.plist cannot be read is skipped instead of refusing the run then broken"""
    readable = _app(tmp_path, "Mail", plistlib.dumps({"CFBundleIdentifier": "com.example.mail"}))
    mystery = _app(tmp_path, "Mystery", plist)
    with pytest.raises(acc.Refused) as refusal:
        acc.same_identifier_processes([readable, mystery], OURS)
    message = str(refusal.value)
    assert f"pid {mystery.pid}" in message and str(tmp_path / "Mystery.app") in message, message
    assert "Mail.app" not in message, message
