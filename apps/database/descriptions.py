"""What every table in the state database is FOR, in one sentence each.

Kept apart from the generator because these are the only hand-written words in
``AGENTS.md``; everything else is introspected from the live database and
cannot go stale.  A table added to the schema without a line here shows up in
the rendered document as ``UNDOCUMENTED`` and fails
``(private path)``, which is the point: a schema an agent
cannot read is a schema an agent will guess at.

Write for a reader who has never seen this project and is about to write a
query against the table.  Say what a ROW is, not what the table is called.
"""

from __future__ import annotations

#: table name -> what one row of it means.
TABLES: dict[str, str] = {
    "pair_transitions": "The saved transition for one ordered pair of tracks, as a plan. Most recent wins. Machine-local.",
    "pair_decisions": "AutoPlay's last lookahead decision for one ordered pair: shortlist, rejections, candidates. Machine-local.",
    "pair_verdicts": "The listener's one-tap good / meh / bad on the transition that ran between one ordered pair. Machine-local.",
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
        "denominator in (private path) counts."
    ),
    "track_availability": (
        "One track's current playability verdict (present, absent, "
        "awaiting_volume or streaming), with the path that was checked and "
        "when. NO row at all means UNKNOWN, which is why `tracks_available` "
        "excludes it: absence of evidence is not evidence of presence."
    ),
    "path_availability": (
        "One rekordbox library path's last stat on THIS machine (materialized "
        "size, mtime_ns, or NULL for not on disk) and when it was taken, keyed by a "
        "resolver namespace so a changed path map or another machine never "
        "reads it. A cache for listing hydration (issue #1037): rows older "
        "than the TTL are stale, and a path with no row is unprobed, not absent."
    ),
    "track_fields": (
        "Current analysed value for one (stable_id, field_name). A later write "
        "replaces the current row regardless of source or confidence; the previous "
        "envelope moves to track_field_history. confidence is reported provenance, "
        "not an arbitration input."
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
        "anlz_cache, vocal_cache, lyrics_cache, karaoke_words). The "
        "CloudSync config UI's per-machine toggles write here."
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
    "sync_write_tokens": (
        "One digested table's current write token, replaced by a trigger on "
        "every row write, so the CloudSync digest can skip re-hashing a "
        "library nothing has touched. Machine-local; it never rides sync."
    ),
    # ----- enrollment: who OWNS a machine (ADR 12) -----------------------
    "machine_owners": (
        "One machine's owner, as ONE hub recorded it. Never synced: a "
        "restored or hostile spoke must not be able to push ownership rows "
        "and adopt machines under last-write-wins like any other row, so "
        "the hub that performed the enrollment is the only writer. Every "
        "row carries the enrolling hub's own machine_id, and a row whose "
        "hub_machine_id is not this hub's live id reads as FOREIGN, not as "
        "ownership -- a hub restored from another machine's backup inherits "
        "this table but not the machine-id file that sits outside the "
        "database."
    ),
    "enrollment_grants": (
        "One short-lived single-use credential minted by an authenticated "
        "operator on the hub and carried, once, to the machine that is "
        "joining. Only the sha256 of the token is stored, never the token, "
        "for the same reason as auth_sessions: a stolen database must not "
        "hand anybody a redeemable credential. A redeemed row is kept, not "
        "deleted: the machine that spent it may present it again until it "
        "expires and gets the same owner back (so enroll is safely re-runnable), "
        "while a replay by another machine, or after expiry, is refused."
    ),
    "machine_credentials": (
        "One machine's sync credential, as the hub that minted it at enroll "
        "recorded it. Only the sha256 is stored; the raw bearer was returned "
        "once to the enrolling machine, which keeps it in <data-dir>/"
        "sync-credential at 0600 and sends it on hello, push, pull, status "
        "and digest. Hub-local and never synced, like machine_owners, so a "
        "spoke cannot push itself a credential."
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
    "analysis_canonical": (
        "Which analysis row is canonical for one track and one selection lane. "
        "Recomputed from every row on each write by one rule (highest producer "
        "semver, tie to in-app over backfill, bench candidates never eligible), "
        "so the answer depends on what was produced and never on write order. "
        "Derived: safe to delete and recompute, never hand-edited."
    ),
    "analysis_queue_batch": (
        "One enqueue call on the native-analysis v1 backfill queue, plus the "
        "admission decision it was planned under: the worker count and band the "
        "spec section 4 memory rule chose from the LONGEST ADMITTED track, and "
        "the measured memory model those numbers came from. Written by "
        "apps.analysis.queue."
    ),
    "analysis_queue_item": (
        "One (track, lane) of backfill work and its state: pending, running, "
        "done, skipped, failed, refused or cancelled. The record write and the "
        "move to done are ONE transaction, which is what makes a resume after a "
        "process kill re-run an interrupted item exactly once and never re-run a "
        "completed one. A refused item is a row with its named reason, never a "
        "silently absent one."
    ),
    "analysis_stale": (
        "Records whose DEPENDENCY moved underneath them (today only key -> "
        "beatgrid). The row keeps its record but stops being eligible for "
        "analysis_canonical until it is recomputed, so a key computed against a "
        "superseded beatgrid never reads as canonical. Written by the queue's "
        "dependency cascade, read by apps.analysis.canonical."
    ),
    # ----- genre findings store (GENRE-03, machine-local) ---------------------
    "genre_findings": (
        "One row per (track, provider): that provider's genre for the track in "
        "the Beatport taxonomy, or status unknown with the reason. Computed "
        "providers (odj-fused, jev, deepseek) are written by "
        "apps.genre_infer.pipeline; any other provider (beatport, agent.*) is "
        "attached from outside through the genre routes, CLI or MCP tools. The "
        "Library shows the first provider in the configured precedence with a "
        "genre. Machine-local: not in the sync set."
    ),
    "genre_vectors": (
        "JEV's full probability vector over the genre taxonomy for a track, "
        "kept for the similarity query "
        "(apps.genre_infer.findings.similar_tracks). Only vectors of the "
        "current taxonomy version are read. Machine-local."
    ),
    "genre_search_cache": (
        "The web-search snippets fetched for one frozen query string, so a "
        "track is searched (and paid for) once. Keyed on the query and its "
        "query version. Machine-local."
    ),
    "genre_config": (
        "Genre settings as key/value JSON: the display precedence, auto_infer, "
        "write_file_tags and write_roots. Read by "
        "apps.genre_infer.findings.read_config; absent keys take the defaults. "
        "Machine-local."
    ),
    "genre_tag_writes": (
        "The undo journal for genre tags written into audio files (GENRE-05): "
        "what was written where, the byte backup that restores it, and whether "
        "it was undone. Written by apps.genre_infer.tag_write before the file "
        "is replaced. Machine-local."
    ),
    "analysis_source_default": (
        "The persisted per-lane source (rbx or own), one row per selection lane, "
        "absent until that lane is promoted. This is what a PROMOTION writes and "
        "the only half of the source selection that survives a relaunch; the "
        "PARITY-02 dev toggle is in-memory and is deliberately not stored."
    ),
    "analysis_projection": (
        "Own-analysis scalars (bpm, key, loudness_lufs, loudness_dbtp, "
        "key_change_count, tempo_change_count) read at query time when a lane's "
        "source is `own`, rebuilt whenever that track's canonical pointer moves. "
        "This is the ONLY place own values live: they are never written into "
        "track_fields, so nothing here reaches track_field_history or the sync "
        "path. `value` is deliberately typeless so numbers stay numbers. Read it "
        "through apps.analysis.selection.effective_fields, never directly."
    ),
    "track_energy_segments": (
        "One contiguous stretch of one track's timeline carrying an energy "
        "rating of 1 to 10, as a single source heard it, ordered by `seq`. "
        "A track has many rows per source; the series IS the shape of the "
        "track, so reading one row alone tells you almost nothing."
    ),
    "unmatched_source_analysis": (
        "One field value a source offered that could not be attached to any "
        "track, kept with the identifying metadata it arrived with (title, "
        "artist, isrc, source path) and the reason it failed to land "
        "(no_candidate, ambiguous_candidates, lost_collision). This is how "
        "analysis for audio we do not have yet survives until the track "
        "turns up."
    ),
    "analysis_field_verification": (
        "One verdict on whether a given source's values for a given field "
        "can be trusted, and on what basis (cross_source, single_source or "
        "unverified). What the equivalence gate consults before letting that "
        "source's analysis be promoted onto a track."
    ),
    # ----- lyrics --------------------------------------------------------
    "lyric_verdict": (
        "One track's karaoke lyrics standing: whether it has vocals worth "
        "aligning (vocal, sparse, no-lyrics, unknown), where the text came "
        "from, how much of it aligned, how suspect the alignment looked, and "
        "the sha256 of the word-timing artifact that carries the timings "
        "themselves. `override` is the human's answer and beats the computed "
        "`verdict` -- read the two together, never `verdict` alone. Synced, "
        "and deleted only by tombstone: the licensing purge stamps "
        "`deleted_at` so peers stop hydrating the words too."
    ),
    # ----- feedback --------------------------------------------------------
    "playlist_section": "A visual separator inside a playlist or set; it does nothing functional.",
    "tag_type": "A kind of tag (general, instrument, mood) and where it shows.",
    "tag_meta": "Type and colour for one tag name. Tag values stay in My Tag.",
    "tag_synonym": "A term that should suggest an existing tag.",
    "work": "A musical work that groups versions of a track.",
    "work_member": "One track or virtual edit linked to a work.",
    "work_dismissal": "A pair the user said is not a version of each other.",
    "work_pref": "Which member a work shows first. Display only.",
    "track_edit": "A non-destructive playback window and FX parameters over one source track; no audio is copied or changed.",
    "library_completeness_config": (
        "Singleton row for whether this library expects HQ stems and a lyric "
        "match on every track that can have them. Machine-local until "
        "CloudSync attaches (parent #759)."
    ),
    "undo_journal": "Before and after snapshots of library organization writes, per undo session. Machine-local.",
    "feedback_pins": (
        "One in-app feedback comment pin, shared across every machine on "
        "the same CloudSync hub. `doc` is the pin exactly as "
        "`GET /api/v1/feedback/comments` returns it; `updated_at` is when it "
        "was last edited (not when it was synced), which is what the "
        "last-writer-wins merge orders on. A set `deleted_at` means the pin "
        "was archived somewhere and stays archived everywhere."
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
    # ----- set target and skip flags (LIBUX-8679) ------------------------
    "set_targets": "Optional target length of a playlist shown as a set.",
    "set_skips": "One track marked skip-in-set within a playlist set.",
    "crowd_request_session": (
        "The one current crowd request session: whether it is open, which "
        "catalog playlist guests may pick from, whether they may type a "
        "message, and the per-phone cooldown."
    ),
    "crowd_requests": (
        "One guest track request or short message, with the requester name, "
        "a sha256 requester_key (never an address), vote count and status."
    ),
    # ----- play orders ---------------------------------------------------
    "play_orders": "One ordered sequence of tracks prepared for performance.",
    "play_order_entries": "One track's slot within one play order.",
    "play_orders_schema_meta": (
        "Migration bookkeeping for the play-order tables, which arrived with "
        "their own ladder before the consolidated schema existed."
    ),
    "transition_try_jobs": "One run of the transition try-er for a pair of tracks, with its candidates.",
    "transition_picks": "The transition a person or agent chose from one try-er run.",
    "transition_try_schema_meta": "Migration bookkeeping for the transition try-er tables.",
    "playlist_sets": (
        "SET-05 named performance set for a playlist, with its own play count."
    ),
    "playlist_set_entries": "One track slot in a playlist_sets snapshot.",
    "playlist_set_runs": (
        "Practice or performance run for a playlist set; performance increments play_count."
    ),
    "playlist_sets_schema_meta": (
        "Migration bookkeeping for the playlist_sets tables (SET-05)."
    ),
    "playlist_component_slots": "One slot (opener, main, ending...) in a playlist's route order.",
    "playlist_components": "A named, tagged run of tracks inside a playlist (SET-6586 component).",
    "playlist_component_entries": "One track position within a playlist component.",
    "playlist_component_tags": "One free user tag on a playlist component (not MyTag).",
    "playlist_set_parts": "One component on a playlist set's route, in order.",
    "playlist_set_run_entries": "One track a playlist set run actually played, frozen at record time.",
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
