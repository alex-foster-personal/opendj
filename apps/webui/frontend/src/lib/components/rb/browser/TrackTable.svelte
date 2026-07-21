<script module lang="ts">
	/** Sortable column keys (client-side sort - ordering is not server-provided). */
	export type SortKey =
		| 'order'
		| 'title'
		| 'artist'
		| 'key'
		| 'bpm'
		| 'rating'
		| 'comments'
		| 'time'
		| 'genre';
</script>

<script lang="ts">
	// Browser track table (SCREENSHOT-SPEC 5c). Columns in screenshot order:
	// funnel | cloud | # | Preview | Artwork | Track Title | Artist | K | B |
	// Rating | Comments | Time | Genre. Rows lazily hydrate rb-meta + preview
	// waveform via IntersectionObserver (only visible rows fetch). Row states:
	// green title+artist = loaded on a deck; blue full row = selected.
	// No virtualization at v1: parent caps rows at 500 (see PARITY-TODO).
	import { artworkUrl } from '$lib/rb/api-rb';
	import type { DeckId, TrackRow } from '$lib/rb/types';
	import PreviewStrip from './PreviewStrip.svelte';
	import RatingStars from './RatingStars.svelte';

	const DECKS: DeckId[] = [1, 2, 3, 4];

	let {
		rows,
		selectedId,
		loadedIds,
		kinds,
		sortKey,
		sortDir,
		truncated,
		emptyMessage,
		onsort,
		onselectrow,
		onloadrow,
		onrate,
		onrowvisible
	}: {
		rows: TrackRow[];
		selectedId: string | null;
		loadedIds: Set<string>;
		kinds: Record<string, 'tri' | 'mono'>;
		sortKey: SortKey | null;
		sortDir: 1 | -1;
		truncated: boolean;
		emptyMessage: string | null;
		onsort: (key: SortKey) => void;
		onselectrow: (row: TrackRow) => void;
		/** deck null = load onto lowest free deck (double-click). */
		onloadrow: (row: TrackRow, deck: DeckId | null) => void;
		onrate: (row: TrackRow, next: number) => void;
		onrowvisible: (row: TrackRow) => void;
	} = $props();

	// ------------------------------------------- lazy-hydration observer
	// One-shot per row element: fetch fires the first time a row scrolls into
	// view (ancestor overflow clipping is honoured by IntersectionObserver, so
	// root null is correct for the scrolling table wrap).
	const _rowByEl = new WeakMap<Element, TrackRow>();
	let _observer: IntersectionObserver | null = null;

	function _ensureObserver(): IntersectionObserver | null {
		if (typeof IntersectionObserver === 'undefined') return null; // SSR guard
		if (_observer === null) {
			_observer = new IntersectionObserver(
				(entries) => {
					for (const entry of entries) {
						if (!entry.isIntersecting) continue;
						const row = _rowByEl.get(entry.target);
						_observer?.unobserve(entry.target);
						if (row !== undefined) onrowvisible(row);
					}
				},
				{ rootMargin: '120px 0px' }
			);
		}
		return _observer;
	}

	function observeRow(node: HTMLElement, row: TrackRow): { destroy(): void } {
		_rowByEl.set(node, row);
		_ensureObserver()?.observe(node);
		return {
			destroy(): void {
				_observer?.unobserve(node);
			}
		};
	}

	$effect(() => {
		return () => {
			_observer?.disconnect();
		};
	});

	// ----------------------------------------------------- cell formatting
	function _fmtTime(ms: number | null): string {
		if (ms === null) return '';
		const total = Math.round(ms / 1000);
		const m = Math.floor(total / 60);
		const s = total % 60;
		return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}

	function _fmtBpm(bpm: number | null): string {
		// Screenshot truncates to '132.' - one decimal is the sanctioned improvement.
		return bpm === null ? '' : bpm.toFixed(1);
	}

	function _hideBrokenImg(event: Event): void {
		(event.currentTarget as HTMLImageElement).style.display = 'none';
	}
</script>

{#snippet sortableTh(key: SortKey, label: string)}
	<th class={`h-${key}`}>
		<button class="th-btn" onclick={() => onsort(key)} title={`Sort by ${label}`}>
			<span>{label}</span>
			{#if sortKey === key}
				<span class="arrow">{sortDir === 1 ? '▲' : '▼'}</span>
			{/if}
		</button>
	</th>
{/snippet}

<div class="tt-root">
	<div class="table-wrap">
		<table>
			<colgroup>
				<col class="w-funnel" />
				<col class="w-cloud" />
				<col class="w-order" />
				<col class="w-preview" />
				<col class="w-art" />
				<col class="w-title" />
				<col class="w-artist" />
				<col class="w-key" />
				<col class="w-bpm" />
				<col class="w-rating" />
				<col class="w-comments" />
				<col class="w-time" />
				<col class="w-genre" />
			</colgroup>
			<thead>
				<tr>
					<th class="h-icon" title="filter - not implemented, see PARITY-TODO">
						<svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true">
							<path d="M2 3h12l-4.5 5v5l-3-1.5V8z" fill="currentColor" />
						</svg>
					</th>
					<th class="h-icon" title="cloud/streaming flag">
						<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
							<path
								d="M4.5 12a3 3 0 0 1-.4-5.97A4 4 0 0 1 12 6.5 2.75 2.75 0 0 1 11.5 12z"
								fill="currentColor"
							/>
						</svg>
					</th>
					{@render sortableTh('order', '#')}
					<th class="h-preview">Preview</th>
					<th class="h-art">Artwork</th>
					{@render sortableTh('title', 'Track Title')}
					{@render sortableTh('artist', 'Artist')}
					{@render sortableTh('key', 'K')}
					{@render sortableTh('bpm', 'B')}
					{@render sortableTh('rating', 'Rating')}
					{@render sortableTh('comments', 'Comments')}
					{@render sortableTh('time', 'Time')}
					{@render sortableTh('genre', 'Genre')}
				</tr>
			</thead>
			<tbody>
				{#each rows as row (`${row.stable_id}:${row.order}`)}
					<!-- key includes order: playlists CAN repeat a track -->
					<!-- svelte-ignore a11y_click_events_have_key_events -->
					<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
					<tr
						use:observeRow={row}
						class:rb-row-selected={row.stable_id === selectedId}
						class:loaded={loadedIds.has(row.stable_id)}
						onclick={() => onselectrow(row)}
						ondblclick={() => onloadrow(row, null)}
					>
						<td class="c-funnel"></td>
						<td class="c-cloud">
							{#if row.rb_meta !== null && row.rb_meta.is_streaming}
								<span
									class="cloud"
									title="streaming track (tidal/soundcloud/spotify) - deck load not implemented, see PARITY-TODO"
								>
									<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
										<path
											d="M4.5 12a3 3 0 0 1-.4-5.97A4 4 0 0 1 12 6.5 2.75 2.75 0 0 1 11.5 12z"
											fill="currentColor"
										/>
									</svg>
								</span>
							{:else if row.rb_meta !== null && !row.rb_meta.file_exists}
								<span class="missing" title="audio file missing on disk">!</span>
							{/if}
						</td>
						<td class="c-order">{row.order}</td>
						<td class="c-preview">
							<PreviewStrip preview={row.preview} kind={kinds[row.stable_id] ?? null} />
							<span class="deck-btns">
								{#each DECKS as d (d)}
									<button
										title={`Load onto deck ${d}`}
										onclick={(e) => {
											e.stopPropagation();
											onloadrow(row, d);
										}}
										ondblclick={(e) => e.stopPropagation()}
									>
										{d}
									</button>
								{/each}
							</span>
						</td>
						<td class="c-art">
							<span class="art-slate" aria-hidden="true"></span>
							{#if row.rb_meta !== null && row.rb_meta.artwork_available}
								<img
									src={artworkUrl(row.stable_id, 's')}
									alt=""
									loading="lazy"
									onerror={_hideBrokenImg}
								/>
							{/if}
						</td>
						<td class="c-title" class:rb-row-loaded={loadedIds.has(row.stable_id)} title={row.title ?? ''}>
							{row.title ?? ''}
						</td>
						<td class="c-artist" class:rb-row-loaded={loadedIds.has(row.stable_id)} title={row.artist ?? ''}>
							{row.artist ?? ''}
						</td>
						<td class="c-key">{row.key ?? ''}</td>
						<td class="c-bpm">{_fmtBpm(row.bpm)}</td>
						<td class="c-rating">
							<RatingStars rating={row.rating} onrate={(n) => onrate(row, n)} />
						</td>
						<td class="c-comments" title={row.comments ?? ''}>{row.comments ?? ''}</td>
						<td class="c-time">{_fmtTime(row.duration_ms)}</td>
						<td class="c-genre">{row.rb_meta?.genre ?? ''}</td>
					</tr>
				{/each}
			</tbody>
		</table>
		{#if rows.length === 0 && emptyMessage !== null}
			<div class="empty">{emptyMessage}</div>
		{/if}
	</div>
	{#if truncated}
		<div class="truncated-note">
			showing first 500 rows - list truncated (no virtualization at v1, see PARITY-TODO)
		</div>
	{/if}
</div>

<style>
	.tt-root {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-height: 0;
	}
	.table-wrap {
		flex: 1;
		min-height: 0;
		overflow: auto;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		table-layout: fixed;
		font-size: var(--rb-fs-browser);
	}

	/* column widths (screenshot proportions) */
	.w-funnel { width: 20px; }
	.w-cloud { width: 24px; }
	.w-order { width: 34px; }
	.w-preview { width: 122px; }
	.w-art { width: 30px; }
	.w-title { width: auto; }
	.w-artist { width: 16%; }
	.w-key { width: 34px; }
	.w-bpm { width: 46px; }
	.w-rating { width: 80px; }
	.w-comments { width: 12%; }
	.w-time { width: 48px; }
	.w-genre { width: 10%; }

	thead th {
		position: sticky;
		top: 0;
		z-index: 1;
		height: 20px;
		padding: 0 6px;
		background: var(--rb-panel-raised);
		border-bottom: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
		font-weight: 400;
		text-align: left;
		white-space: nowrap;
	}
	.h-icon {
		text-align: center;
	}
	.th-btn {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		padding: 0;
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-browser);
		cursor: pointer;
	}
	.th-btn:hover {
		color: var(--rb-text);
	}
	.arrow {
		font-size: 7px;
	}

	tbody tr {
		height: 22px;
		cursor: default;
	}
	tbody tr:hover:not(.rb-row-selected) {
		background: var(--rb-panel-raised);
	}
	td {
		padding: 0 6px;
		border-bottom: 1px solid #131519;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		vertical-align: middle;
	}
	.c-order,
	.c-bpm,
	.c-time {
		text-align: right;
		font-variant-numeric: tabular-nums;
		color: var(--rb-text-dim);
	}
	.c-key {
		color: var(--rb-text-dim);
	}
	.c-cloud {
		text-align: center;
		color: var(--rb-text-dim);
	}
	.missing {
		color: var(--rb-red);
		font-weight: 600;
	}
	.c-comments,
	.c-genre {
		color: var(--rb-text-dim);
	}

	/* preview cell hosts the hover deck-load buttons */
	.c-preview {
		position: relative;
	}
	.deck-btns {
		display: none;
		position: absolute;
		top: 2px;
		right: 4px;
		gap: 2px;
	}
	tbody tr:hover .deck-btns {
		display: inline-flex;
	}
	.deck-btns button {
		width: 16px;
		height: 16px;
		padding: 0;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-size: 9px;
		line-height: 1;
		cursor: pointer;
	}
	.deck-btns button:hover {
		background: var(--rb-accent);
		color: #fff;
	}

	.c-art {
		position: relative;
		padding: 2px 6px;
	}
	.art-slate {
		display: block;
		width: 18px;
		height: 18px;
		background: #22262c;
		border: 1px solid var(--rb-border);
	}
	.c-art img {
		position: absolute;
		top: 2px;
		left: 6px;
		width: 18px;
		height: 18px;
		object-fit: cover;
	}

	.empty {
		padding: 24px;
		text-align: center;
		color: var(--rb-text-dim);
	}
	.truncated-note {
		flex: none;
		padding: 2px 8px;
		border-top: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
	}
</style>
