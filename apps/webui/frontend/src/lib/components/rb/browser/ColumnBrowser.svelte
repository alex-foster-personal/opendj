<script module lang="ts">
	import type { ColumnSourceRow } from './column-buckets';
	import type { FileAvailabilityStatus } from '$lib/rb/api-rb';

	/** Minimal track shape the Miller-column browser needs - deliberately
	 * NOT the full BrowserRow (strip/rb_meta/revealed are table-only
	 * concerns this component never touches). */
	export interface ColumnTrackRow extends ColumnSourceRow {
		stable_id: string;
		title: string | null;
		/** null only while file_availability is AVAILABILITY_PENDING. */
		file_exists: boolean | null;
		file_availability: FileAvailabilityStatus;
		is_streaming: boolean | null;
	}
</script>

<script lang="ts">
	import { rowRendersUnavailable } from './browser-row-wire';
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
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import type { DeckId } from '$lib/rb/deck-slots';
	import {
		albumBuckets,
		artistBuckets,
		filterByColumn,
		type ColumnBucket,
		type ColumnSelector
	} from './column-buckets';
	import VirtualList from './VirtualList.svelte';
	import { fetchAllPages } from './virtual-window';

	// Matches `.row { height: 18px; }` below - see VirtualList's rowHeight doc.
	const ROW_HEIGHT = 18;

	let {
		selectedId,
		onselecttrack,
		onloadtrack
	}: {
		/** The ACTIVE pane's selection (PaneStore.selected_id) - Column View
		 * is one shared left-panel component across all 4 panes (it does not
		 * remount on pane-tab switches), so it must reflect whichever pane is
		 * current rather than keep its own selection state; otherwise
		 * highlighting desyncs the moment the user switches panes or selects
		 * a row in TrackTable instead. */
		selectedId: string | null;
		onselecttrack?: ((row: ColumnTrackRow) => void) | undefined;
		/** deck null = load onto lowest free deck (double-click), matching
		 * TrackTable's convention. */
		onloadtrack?: ((row: ColumnTrackRow, deck: DeckId | null) => void) | undefined;
	} = $props();

	let rows = $state<ColumnTrackRow[] | null>(null); // null = still loading
	let loadError = $state<string | null>(null);
	let artist = $state<ColumnSelector>(undefined);
	let album = $state<ColumnSelector>(undefined);

	onMount(() => {
		void _load();
	});

	async function _load(): Promise<void> {
		try {
			const items = await fetchAllPages((cursor) => listTracksHydrated({ limit: 500, cursor }));
			rows = items.map((t) => ({
				stable_id: t.stable_id,
				// TrackOut spells its nullable fields optional; absent reads the
				// same as null to every column here.
				title: t.title ?? null,
				artist: t.artist ?? null,
				album: t.album ?? null,
				file_exists: t.file_exists,
				file_availability: t.file_availability,
				// CHROME-02: the bulk listing carries is_streaming, so a
				// streaming row here gets the same inert load as the table.
				is_streaming: t.is_streaming
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
		onselecttrack?.(row);
	}

	function _loadTrack(row: ColumnTrackRow, deck: DeckId | null): void {
		onloadtrack?.(row, deck);
	}

	// FR-1: the global 'Hide broken links' preference applies here too -
	// this view self-fetches independently of the panes, so it has to read
	// uiPrefs itself rather than inherit a pre-filtered row set. Filter
	// BEFORE bucketing (same order as pane-contract's filterRows) so a
	// broken track can't still surface as an otherwise-empty artist/album
	// bucket.
	const visibleRows = $derived<ColumnTrackRow[]>(
		rows === null
			? []
			: uiPrefs.hide_broken_links
				? rows.filter((r) => !rowRendersUnavailable(r))
				: rows
	);
	const artistList = $derived<ColumnBucket[]>(artistBuckets(visibleRows));
	const albumList = $derived<ColumnBucket[]>(albumBuckets(visibleRows, artist));
	const artistFilteredCount = $derived(filterByColumn(visibleRows, artist, undefined).length);
	const trackList = $derived<ColumnTrackRow[]>(
		filterByColumn(visibleRows, artist, album) as ColumnTrackRow[]
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
		<!-- 'All' stays pinned above the virtualized scroll region - it is
		     not part of `buckets` and would otherwise need its own window
		     index bookkeeping for a single always-visible row. -->
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
		<VirtualList itemCount={buckets.length} rowHeight={ROW_HEIGHT}>
			{#snippet children(start, end)}
				{#each buckets.slice(start, end) as bucket (bucket.value ?? ' ')}
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
			{/snippet}
		</VirtualList>
	</div>
{/snippet}

<div class="cb-root">
	{#if loadError !== null}
		<div class="cb-status error" title={loadError}>column view unavailable</div>
	{:else if rows === null}
		<div class="cb-status">loading library...</div>
	{:else}
		{@render bucketColumn('Artist', artistList, artist, visibleRows.length, _selectArtist)}
		{@render bucketColumn('Album', albumList, album, artistFilteredCount, _selectAlbum)}
		<div class="col col-tracks">
			<div class="col-head">Track</div>
			{#if trackList.length === 0}
				<div class="row rb-inert">
					<span class="name dim">no tracks</span>
				</div>
			{:else}
				<!-- svelte-ignore a11y_click_events_have_key_events -->
				<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
				<VirtualList itemCount={trackList.length} rowHeight={ROW_HEIGHT}>
					{#snippet children(start, end)}
						{#each trackList.slice(start, end) as row (row.stable_id)}
							<div
								class="row"
								class:selected={selectedId === row.stable_id}
								class:broken={rowRendersUnavailable(row)}
								class:pending={row.file_availability === 'AVAILABILITY_PENDING'}
								title={row.file_exists === false
									? 'cannot load: audio file missing on disk (broken link)'
									: row.file_availability === 'AVAILABILITY_PENDING'
										? 'cannot load: audio on this machine has not been confirmed'
										: undefined}
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
						{/each}
					{/snippet}
				</VirtualList>
			{/if}
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
	/* PERF-RB-01: still being probed - neither dimmed as broken nor loadable. */
	.row.pending {
		font-style: italic;
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
