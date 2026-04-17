# 05-missing-isrc

Covers Appendix B item 4: "Missing ISRC -- forces fingerprint-based
track_id". The expected document leaves isrc null so downstream tools
must fall back to the stable_id fingerprint chain
(apps.open_dj.id.stable_id_for). Neither Serato nor Traktor persists
ISRC in v0.1, so this is the current baseline for every fixture; this
fixture exists to document the behaviour explicitly and to pin it for
future adapters that may learn ISRC round-trip (the schema has it as a
required field in v0.2).
