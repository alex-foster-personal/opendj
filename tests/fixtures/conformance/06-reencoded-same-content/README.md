# 06-reencoded-same-content

Covers Appendix B item 5: "Re-encoded same content (FLAC + 320 MP3) --
same track_id, different content_hash". Two tracks with identical
title/artist/album/bpm/key/duration but distinct file_path extensions
(.flac and .mp3). The canonical track_id should be the same once
stable_id_for lands (keyed on fingerprint, not path); content_hash then
differentiates the physical files. The Phase 16 adapters derive
track_id from path so the conformance harness masks track_id + file_path
anyway, which keeps this round-trip green while stable_id matures.
