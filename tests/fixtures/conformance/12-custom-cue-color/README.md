# 12-custom-cue-color

Covers Appendix B item 12: "Track with custom cue colour that has no
Traktor equivalent -- colour translation". Four hot cues each carry a
24-bit RGB colour that is deliberately not in Traktor's fixed
8-palette (apps/adapters/traktor/mappers.py::cue_color_from_type).
Traktor's round-trip snaps each cue to its palette default for the hot
type, so cue_points.color is masked for the Traktor lane. Serato stores
cue colour in per-file GEOB frames rather than the crate DB so the
adapter drops cues wholesale in Phase 16.
