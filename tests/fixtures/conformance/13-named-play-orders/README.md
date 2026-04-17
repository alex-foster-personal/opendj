# 13-named-play-orders

Covers Appendix B item 13: "Playlist with two named play-orders --
PlayOrder export strategy". Three library tracks plus two named
playlists ("Set order" and "Warmup order") stand in for the two
orderings. The richer spec shape (a single Playlist with multiple
PlayOrder entries, each a named sequence with target-key/target-tempo
hints) is modelled in open-dj/schema/v0.2 $defs/PlayOrder but not yet
in the Phase 16 dataclass layer. Playlist track_ids are intentionally
empty in the expected document because tests/test_conformance.py::_mask
has no path for rewriting adapter-scoped stable_ids that appear inside
playlist entries (see the comment on stable_id_for in each adapter).
When Phase 5 ships stable_id_for and the harness gains a
"playlist.track_ids" identity-mask, this fixture will gain populated
orderings that pin the per-playlist sequence.
