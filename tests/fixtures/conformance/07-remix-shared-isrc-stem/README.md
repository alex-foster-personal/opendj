# 07-remix-shared-isrc-stem

Covers Appendix B item 6: "Remix / edit with shared ISRC-stem --
distinct track_id despite similar fingerprint". Two tracks share the
artist root and album lineage but differ in title, bpm, and duration;
the remix credits DJ Bob alongside Alice (stored here as the comma-
joined single-element form that round-trips through both Serato and
Traktor -- see fixture 04 README for the multi-artist mechanics). When
ISRC support lands both would carry related-but-distinct ISRCs (same
registrant, different recording codes); the stable_id fingerprint
chain must therefore treat them as separate tracks.
