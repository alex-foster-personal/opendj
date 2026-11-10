<script lang="ts">
	// TOPVIEW-05: the Track information overlay the top bar's (i) button opens
	// for the focused deck. Loaded on first open (TrackInfoButton imports it
	// lazily), so it costs the /performance bundle nothing until used.
	//
	// Reads only: GET /api/v1/tracks/{id} for the record and its per-field
	// provenance, GET /api/v1/tracks/{id}/plays for the play counts, and the
	// existing grid-provenance cache for where the beatgrid came from. A
	// failed read is shown with its reason, never as an empty record.
	import { untrack } from 'svelte';
	import { getTrack } from '$lib/api';
	import { deckStates } from '$lib/rb/audio-engine.svelte';
	import { fetchTrackPlays } from '$lib/plays/play-counter';
	import type { DeckId } from '$lib/rb/deck-id';
	import { gridProvenanceFor, requestGridProvenance } from '$lib/rb/grid-provenance.svelte';
	import {
		libraryGridProvenance,
		usbGridProvenance,
		type GridProvenanceRow
	} from '$lib/rb/grid-provenance';
	import { isUsbTrackId } from '$lib/rb/track-source';
	import { trackInfoRows, type TrackInfoRow } from '$lib/rb/track-info';

	let {
		deckId,
		stableId,
		onclose
	}: { deckId: DeckId | null; stableId: string; onclose: () => void } = $props();

	type Load =
		| { state: 'loading' }
		| { state: 'error'; message: string }
		| { state: 'ready'; title: string; rows: TrackInfoRow[]; playsError: string | null };

	let load = $state<Load>({ state: 'loading' });
	let panelEl: HTMLDivElement | undefined = $state();

	async function _load(id: string): Promise<void> {
		load = { state: 'loading' };
		// The grid-provenance route is library-only: a stick id is read off the
		// deck's own ANLZ instead (usbGridProvenance), never asked of it.
		if (!isUsbTrackId(id)) requestGridProvenance(id);
		try {
			const [{ track }, plays] = await Promise.all([getTrack(id), fetchTrackPlays(id)]);
			if (id !== stableId) return; // the deck changed while this was in flight
			load = {
				state: 'ready',
				title: track.title ?? id,
				rows: trackInfoRows(track, plays.plays),
				playsError: plays.error
			};
		} catch (exc) {
			if (id !== stableId) return;
			load = { state: 'error', message: exc instanceof Error ? exc.message : String(exc) };
		}
	}

	$effect(() => {
		// Track stableId only: _load reads the grid-provenance store, and a
		// tracked read would refetch both GETs when that request resolves.
		const id = stableId;
		untrack(() => void _load(id));
	});

	const grid = $derived<GridProvenanceRow>(
		isUsbTrackId(stableId)
			? usbGridProvenance(stableId, deckId !== null ? deckStates[deckId] : undefined)
			: libraryGridProvenance(gridProvenanceFor(stableId))
	);

	$effect(() => {
		panelEl?.focus();
	});

	function onWindowKeyDown(e: KeyboardEvent): void {
		if (e.key === 'Escape') onclose();
	}

	function onWindowPointerDown(e: PointerEvent): void {
		const target = e.target;
		if (target instanceof Node && panelEl?.contains(target)) return;
		// The (i) button toggles the overlay itself; let its own click do that.
		if (target instanceof Element && target.closest('[data-testid="topbar-track-info"]')) return;
		onclose();
	}
</script>

<svelte:window onkeydown={onWindowKeyDown} onpointerdown={onWindowPointerDown} />

<div
	class="track-info"
	role="dialog"
	aria-label={deckId === null
		? 'Track information, library row'
		: `Track information, deck ${deckId}`}
	data-testid="track-info-overlay"
	tabindex="-1"
	bind:this={panelEl}
>
	<header>
		{#if deckId === null}
			<span class="deck-chip" title="Opened from a library or browser row, not a loaded deck"
				>LIBRARY</span
			>
		{:else}
			<span class="deck-chip" title={`The focused deck: deck ${deckId}`}>DECK {deckId}</span>
		{/if}
		<h2 data-testid="track-info-title">
			{load.state === 'ready' ? load.title : 'Track information'}
		</h2>
		<button type="button" class="close" aria-label="Close track information" onclick={onclose}
			>x</button
		>
	</header>
	{#if load.state === 'loading'}
		<p class="status">Loading the track record...</p>
	{:else if load.state === 'error'}
		<p class="status error" data-testid="track-info-error">
			Could not read this track: {load.message}
		</p>
	{:else}
		<dl data-testid="track-info-rows">
			{#each load.rows as row (row.label)}
				<dt>{row.label}</dt>
				<dd title={row.provenance ?? `${row.label}: no provenance stored for this field`}>
					{row.value}
				</dd>
			{/each}
			<dt>Beatgrid</dt>
			<dd data-testid="track-info-grid" title={grid.title}>{grid.value}</dd>
		</dl>
		{#if load.playsError !== null}
			<p class="status error">{load.playsError}</p>
		{/if}
		{#if !isUsbTrackId(stableId)}
			<!-- The full page is library-only: its metadata and write controls refuse stick tracks (Sol P2 4292579888). -->
			<a class="full-page" href={`/track/${encodeURIComponent(stableId)}`}>Open the full track page</a>
		{/if}
	{/if}
</div>

<style>
	.track-info {
		position: fixed;
		top: calc(var(--rb-topbar-h) + 4px);
		right: 12px;
		z-index: 60;
		width: min(420px, calc(100vw - 24px));
		max-height: calc(100vh - var(--rb-topbar-h) - 16px);
		overflow: auto;
		padding: 10px 12px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-browser);
		outline: none;
	}
	header {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 8px;
	}
	h2 {
		flex: 1 1 auto;
		margin: 0;
		font-size: var(--rb-fs-deck-title);
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.deck-chip {
		padding: 1px 6px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		letter-spacing: 0.06em;
	}
	.close {
		background: transparent;
		border: 0;
		color: var(--rb-text-dim);
		cursor: pointer;
		font: inherit;
	}
	dl {
		display: grid;
		grid-template-columns: max-content minmax(0, 1fr);
		gap: 3px 10px;
		margin: 0;
	}
	dt {
		color: var(--rb-text-dim);
	}
	dd {
		margin: 0;
		overflow-wrap: anywhere;
	}
	.status {
		margin: 6px 0 0;
		color: var(--rb-text-dim);
	}
	.status.error {
		color: var(--rb-red);
	}
	.full-page {
		display: inline-block;
		margin-top: 8px;
		color: var(--rb-accent);
	}

	.track-info:focus-visible {
		/* A11Y-11403: keyboard focus ring paired with this file's outline reset. */
		outline: 2px solid var(--rb-focus-ring);
		outline-offset: 2px;
	}
</style>
