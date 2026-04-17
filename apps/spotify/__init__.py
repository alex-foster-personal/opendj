"""Spotify playlist importer + acquisition queue (Phase 9 / CAT-01).

Reads Spotify playlists via OAuth Authorization Code + PKCE (user's own
account) and matches tracks against the local library projected into
the shared state layer (``apps.shared.state``).

Matched tracks land in ``playlists`` + ``playlist_memberships`` with
``vendor="spotify"`` and ``vendor_pl_id=<playlist_id>``. Unmatched
tracks land in the Phase-9 auxiliary ``pending_tracks`` table and are
emitted into CSV + Markdown "acquisition queue" review artifacts.
"""
from __future__ import annotations
