# 14-flip-cue-juggles

Covers Appendix B item 14: "Session with rapid cue juggles (Flip-style)
-- events granularity". Eight hot cues at 500ms intervals stand in for
a Flip-style juggling pattern. The full spec would express this as a
Session with per-event timestamps (open-dj/schema/v0.2 $defs/Session +
$defs/Transition); the Phase 16 dataclass layer does not yet carry
Sessions, so the fixture pins cue granularity instead. Traktor snaps
the cue colour to its palette default (hot = green) and Serato drops
cues entirely per the GEOB split noted in fixture 12.
