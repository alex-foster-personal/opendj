<script lang="ts">
	// LIBWS-02: the Crate Desk inspector. One selected track shows its fields
	// with who set each one (provenance source chip) and edits rating, tags
	// and notes through the same If-Match PATCH the track page uses. Several
	// selected tracks show a summary where a field the rows disagree on reads
	// "Multiple values", never one row's value standing in for all.
	import { ConflictError, getTrack, patchTrack, type Track } from '$lib/api';
	import ConflictDialog from '$lib/components/ConflictDialog.svelte';
	import TempoPrefPopover from '$lib/components/TempoPrefPopover.svelte';
	import { listTrackPlaylists, type TrackPlaylistHit } from '$lib/rb/track-playlists';
	import { libraryWorkspace } from '$lib/library/library-workspace.svelte';
	import { summarizeSelection } from '$lib/library/inspector-summary';
	import TrackInspectorView from './TrackInspectorView.svelte';

	type Patch = { rating?: number; tags_add?: string[]; tags_remove?: string[]; notes?: string };

	const rows = $derived(libraryWorkspace.selected_rows);
	const single = $derived(rows.length === 1 ? rows[0] : null);
	const summary = $derived(rows.length > 1 ? summarizeSelection(rows) : null);
	// Keyed on the id, not the row object: the browser republishes fresh row
	// objects for the same track (a pane refresh after a save), and the load
	// effect below must not reset the inspector, or close an open Tempo pref
	// editor, when the selected track has not changed.
	const singleId = $derived(single?.stable_id ?? null);

	let track = $state<Track | null>(null);
	let etag = $state('');
	let loadError = $state<string | null>(null);
	let playlists = $state<TrackPlaylistHit[] | null>(null);
	let playlistsError = $state<string | null>(null);
	let pendingPatch = $state<Patch | null>(null);
	let conflictServer = $state<Track | null>(null);
	let newTag = $state('');
	/** Why the last save failed, shown inline until the next save or selection. */
	let saveError = $state<string | null>(null);
	let tempoAnchor = $state<{ x: number; y: number } | null>(null);
	let artFailed = $state(false);

	let loadSeq = 0;
	$effect(() => {
		const sid = singleId;
		track = null;
		etag = '';
		loadError = null;
		saveError = null;
		// A conflict belongs to the track it was raised on; keeping it across a
		// selection change would let "Take theirs" or "Keep mine" act on that
		// track while another one is showing.
		conflictServer = null;
		pendingPatch = null;
		settleConflict(null);
		// Every selection change starts a new load, even to none or several
		// tracks, so a save still in flight for the old track never lands here.
		const seq = ++loadSeq;
		playlists = null;
		playlistsError = null;
		tempoAnchor = null;
		artFailed = false;
		if (sid === null) return;
		getTrack(sid).then(
			(res) => {
				if (seq !== loadSeq) return;
				track = res.track;
				etag = res.etag;
			},
			(exc: unknown) => {
				if (seq === loadSeq) loadError = String(exc);
			}
		);
		listTrackPlaylists(sid).then(
			(hits) => {
				if (seq === loadSeq) playlists = hits;
			},
			(exc: unknown) => {
				if (seq === loadSeq) playlistsError = String(exc);
			}
		);
	});

	// A 409 holds the save queue until the user answers the dialog, so a save
	// queued behind it never runs on the conflict etag or takes over its
	// pendingPatch. The answer is the patch to send next (null sends nothing).
	let answerConflict: ((next: Patch | null) => void) | null = null;
	function settleConflict(next: Patch | null): void {
		const answer = answerConflict;
		answerConflict = null;
		answer?.(next);
	}

	// Saves run one at a time: each PATCH needs the etag the previous one
	// returned, and each owns its pendingPatch until it settles. A save queued
	// for a track the selection has since left is dropped.
	let saveChain: Promise<void> = Promise.resolve();
	function applyPatch(patch: Patch): Promise<void> {
		const seq = loadSeq;
		const run = saveChain.then(() => (seq === loadSeq ? savePatch(patch) : undefined));
		saveChain = run.catch(() => {});
		return run;
	}

	async function savePatch(patch: Patch): Promise<void> {
		if (track === null) return;
		// The save is for THIS track. The selection can move while it is in
		// flight, so the answer must never land on whatever is selected by then:
		// the row is captured now, and the inspector's own state is written only
		// if the same load is still showing.
		const seq = loadSeq;
		const row = single !== null && single.stable_id === track.stable_id ? single : null;
		pendingPatch = patch;
		saveError = null;
		try {
			const res = await patchTrack(track.stable_id, etag, patch);
			// Keep the browser row in step so the table shows the same value.
			// Rating is the only field this inspector saves that the row carries:
			// tags have no browser column, and notes are not the Comments
			// column (that is the separate `comments` field).
			if (row !== null && patch.rating !== undefined) row.rating = res.track.rating ?? null;
			if (row !== null && row.etag !== '') row.etag = res.etag;
			if (seq !== loadSeq) return;
			track = res.track;
			etag = res.etag;
			pendingPatch = null;
		} catch (exc) {
			if (seq !== loadSeq) return;
			if (exc instanceof ConflictError) {
				conflictServer = exc.current;
				etag = exc.etag;
				const next = await new Promise<Patch | null>((resolve) => {
					answerConflict = resolve;
				});
				if (next !== null && seq === loadSeq) await savePatch(next);
			} else {
				saveError = String(exc);
			}
		}
	}

	function keepMine(): void {
		const patch = pendingPatch;
		conflictServer = null;
		settleConflict(patch);
	}

	function takeTheirs(): void {
		if (conflictServer !== null) track = conflictServer;
		conflictServer = null;
		pendingPatch = null;
		settleConflict(null);
	}

	function mergeTags(): void {
		if (conflictServer === null || pendingPatch?.tags_add === undefined) {
			takeTheirs();
			return;
		}
		const merged = Array.from(new Set([...conflictServer.tags, ...pendingPatch.tags_add]));
		conflictServer = null;
		settleConflict({ tags_add: merged });
	}

	function addTag(): void {
		const tag = newTag.trim();
		if (tag === '') return;
		newTag = '';
		void applyPatch({ tags_add: [tag] });
	}
</script>

<TrackInspectorView
	rowCount={rows.length}
	paneTitle={libraryWorkspace.pane_title}
	{summary}
	{single}
	{track}
	{loadError}
	{saveError}
	{playlists}
	{playlistsError}
	{artFailed}
	bind:newTag
	{applyPatch}
	{addTag}
	onArtError={() => (artFailed = true)}
	onTempoPref={(x, y) => (tempoAnchor = { x, y })}
/>

{#if tempoAnchor !== null && single !== null}
	<TempoPrefPopover
		stableId={single.stable_id}
		x={tempoAnchor.x}
		y={tempoAnchor.y}
		onclose={() => (tempoAnchor = null)}
	/>
{/if}

{#if conflictServer !== null}
	<ConflictDialog
		mine={pendingPatch ?? {}}
		current={conflictServer}
		onkeep_mine={keepMine}
		ontake_theirs={takeTheirs}
		onmerge_tags={mergeTags}
	/>
{/if}
