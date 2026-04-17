# 10-bpm-provenance-conflict

Covers Appendix B item 10: "Track with all four vendors analysing BPM
differently -- provenance stress test". The expected scalar bpm (123.45)
is the canonical open-dj-tool value; the spec's ProvenanceValue envelope
(open-dj/schema/v0.2 $defs/ProvenanceValue) would additionally carry
per-vendor readings like {mik: 123.45, rekordbox: 124.0, serato: 123.5,
traktor: 123.47, djay: 124.0}. Phase 16's dataclass layer exposes bpm as
a scalar, so this fixture pins the canonical value and defers full
provenance round-trip to the JCS edge (apps.open_dj.provenance). The
'bpm' drop rounds to 2dp, which absorbs the small inter-vendor noise.
