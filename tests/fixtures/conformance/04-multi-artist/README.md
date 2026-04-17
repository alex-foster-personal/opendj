# 04-multi-artist

Covers Appendix B item 3: "Multi-artist (3+ artists, with feat.)". The
expected document carries three artists joined by commas as a single
string ("Alice, Bob, Charlie") because neither adapter agrees on a
multi-element split in Phase 16: Serato's database V2 joins on " & " on
write, and Traktor's _split_artists() always splits " & " / "; " /
" feat. " / " ft. " on read. Authoring the expected as a comma-joined
single element keeps the fixture round-trip-green across both adapters
while still pinning the multi-artist scenario for downstream tools
that do recognise multi-artist lists. When tests/test_conformance.py
grows an 'artists' mask, this fixture will migrate to the proper
multi-element form and document the Serato join as a lossy drop.
