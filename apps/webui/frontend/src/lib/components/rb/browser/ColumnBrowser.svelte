<script module lang="ts">
	import type { ColumnSourceRow } from './column-buckets';

	/** Minimal track shape the Miller-column browser needs - deliberately
	 * NOT the full BrowserRow (strip/rb_meta/revealed are table-only
	 * concerns this component never touches). */
	export interface ColumnTrackRow extends ColumnSourceRow {
		stable_id: string;
		title: string | null;
		file_exists: boolean;
		is_streaming: boolean | null;
	}
</script>

<script lang="ts">
	// Column view (browser-surface unit, Miller-column lane, PlaylistTree's
	// long-inert 'Column View' tab). Self-contained like PlaylistTree's
	// smartlist fetch: this component fetches the WHOLE library (artist +
	// album span every track, not just the active pane's loaded rows) via
	// the same cursor-walking helper the virtualization lane uses, and
	// exposes optional onselecttrack/onloadtrack callbacks so the browser
	// integrator can wire deck-load without this component depending on
	// BrowserPanel's audio-engine plumbing.
	//
	// Artist -> Album (not Genre -> Artist): the bulk /tracks listing this
	// component fetches carries artist + album on every row (base Track
	// fields) but does NOT carry genre - see column-buckets.ts for why a
	// genre column would be a fake column here (always-null, house rule).
	import { onMount } from 'svelte';
	import { listTracksHydrated, RbApiError } from '$lib/rb/api-rb';
	import type { DeckId } from '$lib/rb/types';
	import {
		albumBuckets,
		artistBuckets,
		filterByColumn,
		type ColumnBucket,
		type ColumnSelector
	} from './column-buckets';
	import { fetchAllPages } from './virtual-window';

	let {
		onselecttrack,
		onloadtrack
	}: {
		onselecttrack?: (row: ColumnTrackRow) => void;
		/** deck null = load onto lowest free deck (double-click), matching
		 * TrackTable's convention. */
		onloadtrack?: (row: ColumnTrackRow, deck: DeckId | null) => void;
	} = $props();

	let rows = $state<ColumnTrackRow[] | null>(null); // null = still loading
	let loadError = $state<string | null>(null);
	let artist = $state<ColumnSelector>(undefined);
	let album = $state<ColumnSelector>(undefined);
	let selectedId = $state<string | null>(null);

	onMount(() => {
		void _load();
	});

	async function _load(): Promise<void> {
		try {
			const items = await fetchAllPages((cursor) => listTracksHydrated({ limit: 500, cursor }));
			rows = items.map((t) => ({
				stable_id: t.stable_id,
				title: t.title,
				artist: t.artist,
				album: t.album,
				file_exists: t.file_exists,
				// Bulk listing has no is_streaming (same gap as All Tracks
				// table rows before their lazy rb-meta hydrates, contract
				// point 1) - null here means the SAME "unknown, treat as
				// loadable" fallback loadRow's ?? chain already applies to
				// unhydrated table rows, not a new risk this view invents.
				is_streaming: null
			}));
		} catch (exc) {
			loadError = exc instanceof RbApiError ? exc.code : String(exc);
		}
	}

	function _selectArtist(v: ColumnSelector): void {
		artist = v;
		album = undefined;
	}

	function _selectAlbum(v: ColumnSelector): void {
		album = v;
	}

	function _selectTrack(row: ColumnTrackRow): void {
		selectedId = row.stable_id;
		onselecttrack?.(row);
	}

	function _loadTrack(row: ColumnTrackRow, deck: DeckId | null): void {
		onloadtrack?.(row, deck);
	}

	const artistList = $derived<ColumnBucket[]>(rows === null ? [] : artistBuckets(rows));
	const albumList = $derived<ColumnBucket[]>(rows === null ? [] : albumBuckets(rows, artist));
	const artistFilteredCount = $derived(
		rows === null ? 0 : filterByColumn(rows, artist, undefined).length
	);
	const trackList = $derived<ColumnTrackRow[]>(
		rows === null ? [] : (filterByColumn(rows, artist, album) as ColumnTrackRow[])
	);

	function _label(bucket: ColumnBucket): string {
		return bucket.value ?? '(none)';
	}
</script>

{#snippet bucketColumn(
	title: string,
	buckets: ColumnBucket[],
	selected: ColumnSelector,
	allCount: number,
	onpick: (v: ColumnSelector) => void
)}
	<div class="col">
		<div class="col-head">{title}</div>
		<div class="col-scroll">
			<div
				class="row"
				class:selected={selected === undefined}
				role="button"
				tabindex="0"
				onclick={() => onpick(undefined)}
				onkeydown={(e) => {
					if (e.key === 'Enter') onpick(undefined);
				}}
			>
				<span class="name">All</span>
				<span class="count">{allCount}</span>
			</div>
			{#each buckets as bucket (bucket.value ?? ' ')}
				<div
					class="row"
					class:selected={selected === bucket.value}
					role="button"
					tabindex="0"
					onclick={() => onpick(bucket.value)}
					onkeydown={(e) => {
						if (e.key === 'Enter') onpick(bucket.value);
					}}
				>
					<span class="name" title={_label(bucket)}>{_label(bucket)}</span>
					<span class="count">{bucket.count}</span>
				</div>
			{/each}
		</div>
	</div>
{/snippet}

<div class="cb-root">
	{#if loadError !== null}
		<div class="cb-status error" title={loadError}>column view unavailable</div>
	{:else if rows === null}
		<div class="cb-status">loading library...</div>
	{:else}
		{@render bucketColumn('Artist', artistList, artist, rows.length, _selectArtist)}
		{@render bucketColumn('Album', albumList, album, artistFilteredCount, _selectAlbum)}
		<div class="col col-tracks">
			<div class="col-head">Track</div>
			<div class="col-scroll">
				<!-- svelte-ignore a11y_click_events_have_key_events -->
				<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
				{#each trackList as row (row.stable_id)}
					<div
						class="row"
						class:selected={selectedId === row.stable_id}
						class:broken={!row.file_exists}
						role="button"
						tabindex="0"
						onclick={() => _selectTrack(row)}
						ondblclick={() => _loadTrack(row, null)}
						onkeydown={(e) => {
							if (e.key === 'Enter') _selectTrack(row);
						}}
					>
						<span class="name" title={row.title ?? ''}>{row.title ?? ''}</span>
					</div>
				{:else}
					<div class="row rb-inert">
						<span class="name dim">no tracks</span>
					</div>
				{/each}
			</div>
		</div>
	{/if}
</div>

<style>
	.cb-root {
		display: flex;
		height: 100%;
		min-height: 0;
	}
	.cb-status {
		flex: 1;
		padding: 8px 6px;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
	}
	.cb-status.error {
		color: var(--rb-red);
	}
	.col {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-width: 0;
		min-height: 0;
		border-right: 1px solid var(--rb-border);
	}
	.col:last-child {
		border-right: none;
	}
	.col-tracks {
		flex: 1.4;
	}
	.col-head {
		flex: none;
		padding: 2px 6px;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		border-bottom: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
	}
	.col-scroll {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 4px;
		height: 18px;
		padding: 0 6px;
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		cursor: pointer;
		white-space: nowrap;
	}
	.row:hover {
		background: var(--rb-panel-raised);
	}
	.row.selected {
		background: var(--rb-select);
	}
	.row.broken {
		color: var(--rb-text-dim);
	}
	.row.rb-inert {
		cursor: default;
	}
	.row.rb-inert:hover {
		background: transparent;
	}
	.name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.name.dim {
		color: var(--rb-text-dim);
	}
	.count {
		flex: none;
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
</style>
