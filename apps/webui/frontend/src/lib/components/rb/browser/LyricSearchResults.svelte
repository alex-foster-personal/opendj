<script lang="ts">
	/**
	 * Lyric-only search results (Part 3 of #935, issue #1344). Mounted BELOW
	 * TrackTable in BrowserPanel - never inside it, so the primary metadata
	 * results (rendered through TrackTable's virtualized single-row-type
	 * list) are never delayed or reshaped by this secondary, best-effort
	 * lookup.
	 *
	 * `active`/`primarySettled` gate everything through
	 * `createLyricSearchController` (lyric-search.ts): a lyric fetch is
	 * scheduled only once the primary whole-collection search has settled,
	 * debounced on top of that, and superseded responses are dropped. No
	 * hits (including "still empty because nothing has been typed long
	 * enough to settle yet") renders no divider - an honest empty, matching
	 * the on-disk index's own honest-empty convention
	 * (apps.lyrics.search_index.search_documents).
	 *
	 * Read-only by design: clicking a lyric hit to load it into a deck is
	 * follow-up scope, not asked for by #1344's acceptance criteria.
	 */
	import { onDestroy } from 'svelte';

	import { searchLyrics } from '$lib/rb/api-rb';
	import {
		createLyricSearchController,
		EMPTY_LYRIC_SEARCH_STATE,
		type LyricSearchState
	} from '$lib/rb/lyric-search';

	let {
		query,
		active,
		primarySettled,
		onerror
	}: {
		query: string;
		active: boolean;
		primarySettled: boolean;
		onerror: (message: string) => void;
	} = $props();

	const MAX_LYRIC_SEARCH_ROWS = 20;

	let state: LyricSearchState = $state(EMPTY_LYRIC_SEARCH_STATE);

	const controller = createLyricSearchController(
		async (q) => {
			const results = await searchLyrics({ q, limit: MAX_LYRIC_SEARCH_ROWS });
			return { items: results.items, total: results.total };
		},
		(next) => (state = next),
		(message) => onerror(message)
	);

	$effect(() => {
		controller.update(query, primarySettled, active);
	});

	onDestroy(() => controller.cancel());
</script>

{#if state.items.length > 0}
	<div class="lsr-root">
		<div class="lsr-divider" role="separator">
			Lyric matches
			<span class="lsr-count" title="Lyric-only hits for this query, matched against cached synced lyrics, not title or artist">
				{state.total}
			</span>
		</div>
		<ul class="lsr-list">
			{#each state.items as hit (hit.stable_id)}
				<li class="lsr-row">
					<span class="lsr-title">{hit.title ?? 'Untitled'}</span>
					<span class="lsr-artist">{hit.artist ?? 'Unknown artist'}</span>
					<span class="lsr-snippet" title="The matching lyric line, in context">"{hit.match_context}"</span>
				</li>
			{/each}
		</ul>
	</div>
{/if}

<style>
	.lsr-root {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-top: 4px;
	}
	.lsr-divider {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 4px 8px;
		font-family: var(--rb-font);
		font-size: 10px;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--rb-text-dim);
		border-top: 1px solid color-mix(in srgb, #4fb2ff 30%, transparent);
	}
	.lsr-count {
		opacity: 0.7;
	}
	.lsr-list {
		display: flex;
		flex-direction: column;
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.lsr-row {
		display: grid;
		grid-template-columns: 1fr 1fr 2fr;
		gap: 8px;
		padding: 3px 8px;
		font-family: var(--rb-font);
		font-size: 11px;
		color: var(--rb-text);
	}
	.lsr-title {
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.lsr-artist {
		color: var(--rb-text-dim);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.lsr-snippet {
		font-style: italic;
		color: var(--rb-text-dim);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
