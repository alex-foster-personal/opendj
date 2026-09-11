<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { getTrack, patchTrack, ConflictError, type Track } from '$lib/api';
	import StarRating from '$lib/components/StarRating.svelte';
	import ConflictDialog from '$lib/components/ConflictDialog.svelte';
	import ProvenanceTooltip from '$lib/components/ProvenanceTooltip.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import QualityBadge from '$lib/components/rb/QualityBadge.svelte';
	import LyricsPanel from '$lib/components/LyricsPanel.svelte';
	import { fetchRbMeta, RbApiError } from '$lib/rb/api-rb';
	import type { TrackQuality } from '$lib/rb/library-types';

	let track = $state<Track | null>(null);
	let etag = $state<string>('');
	let pendingPatch = $state<Record<string, unknown> | null>(null);
	let conflictServer = $state<Track | null>(null);
	let newTag = $state('');
	// Venue-rung quality rides along on rb-meta (one extra GET on this
	// single-track route; the badge stays absent until it lands, never a guess).
	let quality = $state<TrackQuality | null>(null);

	async function load(): Promise<void> {
		const stable = $page.params.stable_id;
		if (stable === undefined) throw new Error('track route param "stable_id" missing');
		const res = await getTrack(stable);
		track = res.track;
		etag = res.etag;
		try {
			quality = (await fetchRbMeta(stable)).quality;
		} catch (exc) {
			// Since #505 rb-meta answers 200 for a locally imported track, so a
			// 404 here means an unknown stable_id. Kept as a guard rather than
			// removed: an unhandled rejection in onMount is not how that should
			// surface. The badge stays absent (never a guessed rung); anything
			// else is a real failure and still rejects onMount's promise.
			if (!(exc instanceof RbApiError) || exc.status !== 404) throw exc;
		}
	}

	async function applyPatch(patch: Record<string, unknown>): Promise<void> {
		if (!track) return;
		pendingPatch = patch;
		try {
			const res = await patchTrack(track.stable_id, etag, patch);
			track = res.track;
			etag = res.etag;
			pendingPatch = null;
			pushToast('Saved');
		} catch (exc) {
			if (exc instanceof ConflictError) {
				conflictServer = exc.current;
				etag = exc.etag;
			} else {
				pushToast(`Save failed: ${exc}`, 'error');
			}
		}
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

	onMount(load);
</script>

{#if track}
	<a href="/">&larr; back to library</a>
	<h2><ProvenanceTooltip {track} field="title">{track.title ?? '(untitled)'}</ProvenanceTooltip></h2>
	<p><ProvenanceTooltip {track} field="artist">{track.artist ?? ''}</ProvenanceTooltip></p>
	<p>BPM: {track.bpm ?? '?'} · Key: {track.key ?? '?'}</p>
	{#if quality}<p><QualityBadge {quality} /></p>{/if}
	<p>Rating: <StarRating rating={track.rating ?? 0} onchange={(n) => applyPatch({ rating: n })} /></p>

	<h3>Tags</h3>
	<div>
		{#each track.tags as tag}
			<span class="chip">{tag} <button onclick={() => applyPatch({ tags_remove: [tag] })}>×</button></span>
		{/each}
		<input placeholder="Add tag" bind:value={newTag}
			onkeydown={(e) => { if (e.key === 'Enter' && newTag) { applyPatch({ tags_add: [newTag] }); newTag = ''; } }} />
	</div>

	<h3>Notes</h3>
	<textarea rows="4" value={track.notes ?? ''} onblur={(e) => applyPatch({ notes: (e.currentTarget as HTMLTextAreaElement).value })}></textarea>

	<LyricsPanel stableId={track.stable_id} />

	<h3>Actions (coming in Phase 17)</h3>
	<p style="color: var(--muted);">Open in Rekordbox · Open in djay · Show in Finder</p>

	{#if conflictServer}
		<ConflictDialog
			mine={pendingPatch ?? {}}
			current={conflictServer}
			onkeep_mine={keepMine}
			ontake_theirs={takeTheirs}
			onmerge_tags={mergeTags}
		/>
	{/if}
{:else}
	<p>Loading...</p>
{/if}
