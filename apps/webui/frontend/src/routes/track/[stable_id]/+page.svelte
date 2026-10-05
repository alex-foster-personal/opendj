<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { getTrack, patchTrack, ConflictError, type Track } from '$lib/api';
	import RatingStars from '$lib/components/rb/browser/RatingStars.svelte';
	import ConflictDialog from '$lib/components/ConflictDialog.svelte';
	import ProvenanceTooltip from '$lib/components/ProvenanceTooltip.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import QualityBadge from '$lib/components/rb/QualityBadge.svelte';
	import LineLyricsPanel from '$lib/components/LineLyricsPanel.svelte';
	import LyricsPanel from '$lib/components/LyricsPanel.svelte';
	import { fetchRbMeta, RbApiError } from '$lib/rb/api-rb';
	import { lyricEntry, loadLyrics } from '$lib/lyrics/lyrics-cache.svelte';
	import { openStage } from '$lib/lyrics/stage-store.svelte';
	import type { TrackQuality } from '$lib/rb/library-types';
	import TrackActions from '$lib/components/TrackActions.svelte';
	import {
		describeLoadError,
		notesChanged,
		routeLoadError,
		type RouteLoadError
	} from '$lib/route-load-state';

	let track = $state<Track | null>(null);
	let etag = $state<string>('');
	let pendingPatch = $state<Record<string, unknown> | null>(null);
	let conflictServer = $state<Track | null>(null);
	let newTag = $state('');
	// Venue-rung quality rides along on rb-meta (one extra GET on this
	// single-track route; the badge stays absent until it lands, never a guess).
	let quality = $state<TrackQuality | null>(null);

	// Set when the track itself could not be loaded: an unknown id answers 404
	// and reads as "not found"; anything else shows its reason. Either way the
	// page settles instead of sitting on "Loading..." forever.
	let loadError = $state<RouteLoadError | null>(null);
	// The last notes save failure, shown beside the field until a save lands.
	let notesError = $state<string | null>(null);

	async function load(): Promise<void> {
		const stable = $page.params.stable_id;
		if (stable === undefined) {
			loadError = { kind: 'not-found', message: 'no track id in the address' };
			return;
		}
		loadError = null;
		void loadLyrics(stable);
		try {
			const res = await getTrack(stable);
			track = res.track;
			etag = res.etag;
		} catch (exc) {
			loadError = routeLoadError(exc);
			return;
		}
		try {
			quality = (await fetchRbMeta(stable)).quality;
		} catch (exc) {
			// No rekordbox row (404) is the honest "no quality badge" state; any
			// other failure is reported rather than swallowed, and the track
			// itself stays on screen.
			if (!(exc instanceof RbApiError) || exc.status !== 404) {
				pushToast(`Quality badge unavailable: ${describeLoadError(exc)}`, 'error');
			}
		}
	}

	const stageWordCount = $derived(
		track === null ? 0 : (lyricEntry(track.stable_id)?.track?.words.length ?? 0)
	);

	async function applyPatch(patch: Record<string, unknown>): Promise<void> {
		if (!track) return;
		pendingPatch = patch;
		try {
			const res = await patchTrack(track.stable_id, etag, patch);
			track = res.track;
			etag = res.etag;
			pendingPatch = null;
			if ('notes' in patch) notesError = null;
			pushToast('Saved');
		} catch (exc) {
			if (exc instanceof ConflictError) {
				conflictServer = exc.current;
				etag = exc.etag;
			} else {
				const reason = describeLoadError(exc);
				if ('notes' in patch) notesError = reason;
				pushToast(`Save failed: ${reason}`, 'error');
			}
		}
	}

	/** Blur saves the notes only when the text differs from what is saved. */
	function saveNotesOnBlur(next: string): void {
		if (!track || !notesChanged(track.notes, next)) return;
		void applyPatch({ notes: next });
	}

	function keepMine(): void {
		if (pendingPatch) applyPatch(pendingPatch);
		conflictServer = null;
	}

	function takeTheirs(): void {
		if (conflictServer) {
			track = conflictServer;
			conflictServer = null;
			pendingPatch = null;
		}
	}

	function mergeTags(): void {
		if (!conflictServer || !pendingPatch?.tags_add) {
			takeTheirs();
			return;
		}
		const merged = [...conflictServer.tags, ...(pendingPatch.tags_add as string[])];
		applyPatch({ tags_add: Array.from(new Set(merged)) });
		conflictServer = null;
	}

	onMount(() => {
		void load();
		const stable = $page.params.stable_id;
		if (stable !== undefined && new URLSearchParams(location.search).get('stage') === '1') {
			openStage(stable);
		}
	});
</script>

{#if track}
	<a href="/">&larr; back to library</a>
	<h2><ProvenanceTooltip {track} field="title">{track.title ?? '(untitled)'}</ProvenanceTooltip></h2>
	<p><ProvenanceTooltip {track} field="artist">{track.artist ?? ''}</ProvenanceTooltip></p>
	<p>BPM: {track.bpm ?? '?'} · Key: {track.key ?? '?'}</p>
	{#if quality}<p><QualityBadge {quality} /></p>{/if}
	<p>Rating: <RatingStars rating={track.rating ?? 0} onrate={(n) => applyPatch({ rating: n })} /></p>

	<h3>Tags</h3>
	<div>
		{#each track.tags as tag}
			<span class="chip">{tag} <button onclick={() => applyPatch({ tags_remove: [tag] })}>×</button></span>
		{/each}
		<input placeholder="Add tag" bind:value={newTag}
			onkeydown={(e) => { if (e.key === 'Enter' && newTag) { applyPatch({ tags_add: [newTag] }); newTag = ''; } }} />
	</div>

	<h3>Notes</h3>
	<textarea rows="4" value={track.notes ?? ''} onblur={(e) => saveNotesOnBlur((e.currentTarget as HTMLTextAreaElement).value)}></textarea>
	{#if notesError !== null}
		<p class="notes-error" role="alert">Notes not saved: {notesError}. Edit and leave the field to retry.</p>
	{/if}

	{#if stageWordCount > 0}
		<button
			type="button"
			class="stage-open"
			title={`Open the full-screen stage lyrics view - ${stageWordCount} aligned words (close with Esc or the X)`}
			onclick={() => track !== null && openStage(track.stable_id)}
		>
			Stage
		</button>
	{/if}
	<LineLyricsPanel stableId={track.stable_id} />
	<LyricsPanel stableId={track.stable_id} />

	<h3>Actions</h3>
	<TrackActions stableId={track.stable_id} />

	{#if conflictServer}
		<ConflictDialog
			mine={pendingPatch ?? {}}
			current={conflictServer}
			onkeep_mine={keepMine}
			ontake_theirs={takeTheirs}
			onmerge_tags={mergeTags}
		/>
	{/if}
{:else if loadError !== null}
	<a href="/">&larr; back to library</a>
	<div class="load-error" role="alert">
		{#if loadError.kind === 'not-found'}
			<h2>Track not found</h2>
			<p>No track in the library has the id <code>{$page.params.stable_id ?? ''}</code>.</p>
		{:else}
			<h2>Could not load this track</h2>
			<p>{loadError.message}</p>
			<button type="button" onclick={() => void load()}>Retry</button>
		{/if}
	</div>
{:else}
	<p>Loading...</p>
{/if}

<style>
	.notes-error,
	.load-error p {
		color: var(--danger);
	}
</style>
