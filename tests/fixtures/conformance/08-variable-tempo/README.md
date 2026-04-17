# 08-variable-tempo

Covers Appendix B item 7: "Variable-tempo track -- forces explicit
beats[]". The bpm field here is the track-level average (132 BPM); the
per-anchor grid (120 BPM intro accelerating to 144 BPM outro) lives on
Track.beats as a tuple of BeatGridPoint anchors once adapters learn to
round-trip them. The Phase 16 Serato and Traktor adapters currently
drop Track.beats on both read and write, so the conformance harness
exercises only the scalar bpm. The fixture therefore ships with
beats: [] and documents the intent in this README; a 'beats' mask
entry in tests/test_conformance.py::_mask() will formalise the lossy
lane when beatgrid round-trip ships.
