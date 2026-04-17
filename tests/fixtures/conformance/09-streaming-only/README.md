# 09-streaming-only

Covers Appendix B item 9: "Streaming-only track (no audio file) --
file_path + content_hash policy edge case". The file_path here is a
synthetic tidal:// URI rather than a local filesystem path; content_hash
is not yet modelled in the Phase 16 dataclass layer. The harness masks
file_path before JCS comparison so URI-shaped paths round-trip cleanly
even through adapters that expect POSIX paths. When content_hash ships
in apps.open_dj.schema.Track, this fixture will sprout a content_hash
field to exercise the null-file + set-hash policy.
