"""What every table in the state database is FOR, in one sentence each.

Kept apart from the generator because these are the only hand-written words in
``AGENTS.md``; everything else is introspected from the live database and
cannot go stale.  A table added to the schema without a line here shows up in
the rendered document as ``UNDOCUMENTED`` and fails
``tests/database/test_agents_md.py``, which is the point: a schema an agent
cannot read is a schema an agent will guess at.

Write for a reader who has never seen this project and is about to write a
query against the table.  Say what a ROW is, not what the table is called.
"""

from __future__ import annotations

#: table name -> what one row of it means.
TABLES: dict[str, str] = {
    # ----- state_core: the library itself -------------------------------
    "tracks": (
        "One musical work as this project understands it, independent of any "
        "file on disk and of any vendor's id for it. THE anchor table: almost "
        "everything else points here."
    ),
    "track_locations": (
        "One place a track's audio has been seen, so a track can have several "
        "(a local copy, a USB export, a since-deleted path). Absence of a row "
        "means the audio is unresolvable, which is what the 'present' "
        "denominator in docs/library-availability.md counts."
    ),
    "track_fields": (
        "One field value for one track from one adapter, so rekordbox, djay "
        "and serato can each hold a different opinion about the same track "
        "without overwriting each other."
    ),
    "track_field_history": (
        "One prior value of a track field, appended when a field changes. The "
        "audit trail behind any 'why did this BPM move' question."
    ),
    "track_vendor_ids": (
        "One vendor's identifier for one track (a rekordbox id, a Spotify "
        "uri). Kept out of `tracks` so a track surviving a vendor's id change "
        "does not need a rewrite."
    ),
    "playlists": "One playlist or crate, from whichever adapter supplied it.",
    "playlist_memberships": (
        "One track's place in one playlist, carrying the position so ordering "
        "survives a re-import."
    ),
    "adapters": (
        "One external library this project reads or writes (rekordbox, djay, "
        "serato, traktor), and the state of its last sync."
    ),
    "events": (
        "One thing that happened, appended and never updated. The engine's "
        "event bus reads from here."
    ),
    "users": (
        "One Google account that has signed in to this daemon, keyed on the "
        "OIDC `sub` claim rather than the email, because an email can be "
        "reassigned and a `sub` cannot. Sign-in is identity, never "
        "authorisation: the app behaves identically signed out."
    ),
    "auth_sessions": (
        "One browser session for one user. Holds the sha256 of the session "
        "token (never the token) plus the Google refresh token, so the "
        "browser never sees a Google credential. Cascades on user delete."
    ),
    "schema_meta": (
        "One applied migration. The highest `version` is the schema the file "
        "is at; the ladder in apps/shared/state/schema.py replays from there."
    ),
    # ----- sync_infra: CloudSync fleet + changelog -----------------------
    "machines": (
        "One machine that has ever synced, keyed on a uuid4 machine_id "
        "minted at first run and stored outside the database so a "
        "Litestream restore onto different hardware cannot inherit the "
        "old machine's identity. Part of the synced set itself, so any "
        "machine can see the whole fleet."
    ),
    "sync_policies": (
        "One machine's sync mode (pinned, cached, stream, or excluded) and "
        "optional cache budget for one asset kind (audio, stem_bundle, "
        "anlz_cache, vocal_cache). The CloudSync config UI's per-machine "
        "toggles write here."
    ),
    "playlist_pins": (
        "One machine's sync-mode override for one playlist, taking "
        "precedence over that machine's general sync_policies row for the "
        "tracks it contains -- keeping a gig playlist's audio pinned "
        "locally even when the machine otherwise streams."
    ),
    "sync_state": (
        "This machine's push and pull watermarks against one sync peer "
        "(today, always the hub): how far it has pushed into and pulled "
        "from that peer's changelog. Machine-local and never itself "
        "synced -- syncing your own sync watermarks would be incoherent."
    ),
    "hub_changelog": (
        "One row the hub accepted from a spoke's push, appended in a "
        "monotonic AUTOINCREMENT sequence so a spoke's pull watermark is "
        "just the highest seq it has already pulled. Hub-local (agentbox "
        "only); it never rides sync itself."
    ),
    "local_changelog": (
        "One row this spoke appended for its own write to a synced table, "
        "the spoke-local twin of hub_changelog that this machine's push "
        "watermark fences against. Machine-local; it never rides sync "
        "itself."
    ),
    # ----- analysis ------------------------------------------------------
    "analysis": (
        "One track's computed audio analysis (BPM, key, beatgrid, loudness). "
        "Derived data: safe to delete and recompute, never hand-edited."
    ),
    "analysis_events": (
        "One analysis run's outcome, including failures, so a track that "
        "cannot be analysed is distinguishable from one never attempted."
    ),
    # ----- curation ------------------------------------------------------
    "pairings": (
        "One asserted relationship between two tracks (mixes well into, "
        "same vocal, duplicate-ish), with the evidence that produced it."
    ),
    "smartlists": (
        "One saved rule that resolves to a track set at read time. Stores the "
        "rule, never the resulting tracks."
    ),
    # ----- play orders ---------------------------------------------------
    "play_orders": "One ordered sequence of tracks prepared for performance.",
    "play_order_entries": "One track's slot within one play order.",
    "play_orders_schema_meta": (
        "Migration bookkeeping for the play-order tables, which arrived with "
        "their own ladder before the consolidated schema existed."
    ),
    # ----- spotify -------------------------------------------------------
    "spotify_playlist_links": "One mapping between a local playlist and a Spotify playlist.",
    "spotify_playlist_meta": "One Spotify playlist's cached metadata (name, owner, snapshot id).",
    "pending_tracks": (
        "One track seen on Spotify that has no local counterpart yet: the "
        "acquisition queue."
    ),
    # ----- sets ----------------------------------------------------------
    "sets": "One recorded or planned DJ set.",
    "set_events": (
        "One thing that happened during a set (a load, a play, a cue), which "
        "is what play analytics aggregates."
    ),
    # ----- settings ------------------------------------------------------
    "settings": (
        "One key/value setting for this daemon. Machine-local by nature, and "
        "therefore the table CloudSync must NOT blindly replicate."
    ),
    # ----- dedup ---------------------------------------------------------
    "duplicate_clusters": "One group of tracks believed to be the same recording.",
    "track_aliases": "One track's redirect to the cluster survivor after a merge.",
    "tag_provenance": (
        "Where one tag value came from, so a merge can prefer the better "
        "source instead of whichever row won."
    ),
    # ----- launcher ------------------------------------------------------
    "tracks_fts": (
        "Full-text search index over tracks. Derived: rebuildable from "
        "`tracks` and never a source of truth."
    ),
    "tracks_frecency": (
        "One track's recency-and-frequency score, powering launcher ordering. "
        "Derived, and cheap to recompute."
    ),
}

__all__ = ["TABLES"]
