# 15-pairing-nontransitive

Covers Appendix B item 15: "Track with a 3-hop pairing graph
(A -> B -> C, A <-> C) -- pairing non-transitivity". Three tracks
participate in the pairing graph: A pairs into B, B pairs into C, and
A pairs bidirectionally with C. The full spec models these as Pairing
records at the library root (open-dj/schema/v0.2 $defs/Pairing with
from_track_id, to_track_id, direction, source); the Phase 16 dataclass
layer does not yet expose pairings, so this fixture currently pins
only the track vocabulary. When apps.open_dj.schema.OpenDjLibrary gains
a pairings tuple, the expected document will gain three Pairing
entries plus a 'pairings' drop in _mask() for adapters that can't
persist them.
