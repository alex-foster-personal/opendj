"""Phase 4 SYNC-04 Plan 3: RB cue writer tests (unit + fixture DB)."""
from __future__ import annotations

import pytest

from apps.shared.normalised import NormalisedCue
from apps.sync.rb_writer import WriteReport, _kind_to_rb, _msec_to_frame

# write_cues mutates DjmdCue on whatever handle it is handed, so it sits behind
# the one-way rekordbox import gate. This module exercises that MECHANIC
# against a fake db, so it runs with the gate ON (root conftest reads the
# marker). It never touches a real rekordbox target.
pytestmark = [
    pytest.mark.requirement("SYNC-04"),
    pytest.mark.rekordbox_writeback,
]


def test_kind_to_rb_memory():
    kind, active = _kind_to_rb("memory", None, False)
    assert kind == 0 and active is False


def test_kind_to_rb_hot_slot():
    for i in range(8):
        kind, active = _kind_to_rb("hot", i, False)
        assert kind == i + 1
        assert active is False


def test_kind_to_rb_hot_without_index():
    kind, _ = _kind_to_rb("hot", None, False)
    assert kind == 1


def test_kind_to_rb_loop():
    kind, active = _kind_to_rb("loop", None, False)
    assert kind == 4
    assert active is True


def test_kind_to_rb_load():
    kind, _ = _kind_to_rb("load", None, False)
    assert kind == 3


def test_kind_to_rb_unknown_returns_memory():
    kind, _ = _kind_to_rb("unknown", None, False)
    assert kind == 0


def test_msec_to_frame_zero():
    assert _msec_to_frame(0) == 0


def test_msec_to_frame_scales_44_1khz():
    # 1000 ms at 44.1 kHz = 44100 samples; 0.441 heuristic = 441 "frames"
    assert _msec_to_frame(1000) == 441


def test_msec_to_frame_rounds():
    # 12345 * 0.441 = 5444.145 -> round to 5444
    assert _msec_to_frame(12345) == 5444


def test_write_report_default_counters_zero():
    r = WriteReport(content_id="x")
    assert r.inserted == 0
    assert r.updated == 0
    assert r.deleted == 0
    assert r.errors == []


def test_write_cues_smoke_with_fake_db():
    """Smoke test without touching pyrekordbox: supply a duck-typed DB."""
    from apps.sync.rb_writer import write_cues

    class FakeContent:
        UUID = "uuid-1"

    class FakeResult:
        def __init__(self, c):
            self._c = c

        def one(self):
            return self._c

    class FakeCue:
        def __init__(self):
            self.InMsec = None
            self.InFrame = None
            self.InMpegFrame = None
            self.InMpegAbs = None
            self.Kind = None
            self.Color = None
            self.Comment = None
            self.OutMsec = None
            self.OutFrame = None
            self.ActiveLoop = False
            self.ContentUUID = None
            self.ContentID = "1"

    class FakeDB:
        def __init__(self):
            self.cues: list[FakeCue] = []
            self.committed = False

        def get_cue(self, ContentID=None):
            if ContentID is not None:
                return [c for c in self.cues if c.ContentID == str(ContentID)]
            return list(self.cues)

        def get_content(self, ID=None):
            return FakeResult(FakeContent())

        def create_cue(self, **kwargs):
            c = FakeCue()
            for k, v in kwargs.items():
                setattr(c, k, v)
            self.cues.append(c)
            return c

        def delete_cue(self, cue):
            self.cues.remove(cue)

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

    db = FakeDB()
    report = write_cues(
        db,
        "1",
        [
            NormalisedCue(position_msec=0, kind="memory", name="intro"),
            NormalisedCue(position_msec=1000, kind="hot", index=0),
        ],
    )
    assert report.inserted == 2
    assert report.errors == []
    assert db.committed


def test_write_cues_prune_missing_deletes_extra():
    from apps.sync.rb_writer import write_cues

    class FakeCue:
        def __init__(self, msec, kind):
            self.InMsec = msec
            self.InFrame = 0
            self.InMpegFrame = None
            self.InMpegAbs = None
            self.Kind = kind
            self.Color = None
            self.Comment = None
            self.OutMsec = None
            self.OutFrame = None
            self.ActiveLoop = False
            self.ContentUUID = ""
            self.ContentID = "1"

    class FakeContent:
        UUID = "uuid-1"

    class FakeResult:
        def __init__(self, c):
            self._c = c

        def one(self):
            return self._c

    class FakeDB:
        def __init__(self):
            self.cues = [FakeCue(0, 0), FakeCue(5000, 0)]

        def get_cue(self, ContentID=None):
            return list(self.cues)

        def get_content(self, ID=None):
            return FakeResult(FakeContent())

        def create_cue(self, **kwargs):
            c = FakeCue(kwargs.get("InMsec", 0), kwargs.get("Kind", 0))
            self.cues.append(c)
            return c

        def delete_cue(self, cue):
            self.cues.remove(cue)

        def commit(self):
            pass

        def rollback(self):
            pass

    db = FakeDB()
    report = write_cues(
        db,
        "1",
        [NormalisedCue(position_msec=0, kind="memory")],
        prune_missing=True,
    )
    # The cue at 5000 is unmatched; prune_missing deletes it.
    assert report.deleted == 1
    assert len(db.cues) == 1
