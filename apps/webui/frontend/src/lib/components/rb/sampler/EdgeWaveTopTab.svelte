<script lang="ts">
	/**
	 * Top tab for one edge (spec 7.4, mock variant D `.whead`): the 132 px band opening to
	 * 476 px, a dashed tick at rest. Inside: set name, `set n / N` and the paging arrows; the
	 * search box (Enter shows all matches, Esc clears); the library's four filter icons with
	 * their own hover sentences, "compatible" and its caret (`oncompat` opens EdgeCompatPop, the
	 * mock's "Compatible with master" popover on the library's shared prefs); the counts line. Right-click names the shown set (`onrename`).
	 * Width is written by EdgeWave's frame loop.
	 */
	import { countsLine } from '$lib/player/sampler/edge-search';
	import { HEAD_H, REST_W } from '$lib/player/sampler/edge-wave';
	import {
		clearEdgeResults,
		countAllMatches,
		edgeFiltering,
		edgeItems,
		edgeTitle,
		flipSet,
		sampler,
		setEdgeFilter,
		setEdgeSearch,
		showEdgeResults,
		slotMatchesEdge
	} from '$lib/player/sampler/sampler-store.svelte';
	import type { Edge } from '$lib/player/sampler/sampler-types';
	import { LIBRARY_FILTER_LABELS } from '$lib/rb/library-filter-labels';
	import { PLAY_TRIANGLE_PATH } from '$lib/ui/icon-glyphs';
	import './edge-sampler-tokens.css';

	let {
		edge,
		onrename,
		compatOpen,
		oncompat,
		el = $bindable()
	}: {
		edge: Edge;
		onrename: () => void;
		/** The "Compatible with master" popover (EdgeCompatPop, owned by EdgeWave) is open. */
		compatOpen: boolean;
		oncompat: () => void;
		el?: HTMLDivElement | undefined;
	} = $props();

	const ICON_FILTERS = ['broken', 'remixes', 'vocals', 'offline'] as const;
	/** Stroke icons from the mock's `ICONS` table. */
	const ICON_PATHS: Record<(typeof ICON_FILTERS)[number], string> = {
		broken: 'M6.5 9.5l-2 2a2 2 0 01-3-3l2-2M9.5 6.5l2-2a2 2 0 013 3l-2 2M3 1l1.5 2M13 15l-1.5-2',
		remixes: 'M2 5h9l-2-2M14 11H5l2 2',
		vocals: 'M6 3.5a2 2 0 014 0v4a2 2 0 01-4 0zM3.5 8a4.5 4.5 0 009 0M8 12.5v2',
		offline: 'M4.5 12.5h7a3 3 0 00.4-6 4 4 0 00-7.6 1 2.5 2.5 0 00.2 5zM6 9.5l1.5 1.5L10 8'
	};

	const st = $derived(sampler.edges[edge]);
	const head = $derived(edgeTitle(edge));
	const counts = $derived.by(() => {
		if (!edgeFiltering(edge)) return null;
		const here = edgeItems(edge).filter((s) => s !== null && slotMatchesEdge(edge, s)).length;
		return { here, all: countAllMatches(edge) };
	});

	function onKey(ev: KeyboardEvent): void {
		if (ev.key === 'Escape') {
			setEdgeSearch(edge, '');
			clearEdgeResults(edge);
			(ev.currentTarget as HTMLInputElement).blur();
		} else if (ev.key === 'Enter') showEdgeResults(edge);
	}
</script>

<div
	bind:this={el}
	class="whead rest {edge}"
	style:height="{HEAD_H - 2}px"
	style:width="{REST_W}px"
	title={st.results === null
		? `${head.description ? `${head.description}\n` : ''}Right-click to name this set`
		: ''}
	data-edge={edge}
	data-testid="edge-wave-top-{edge}"
	role="group"
	aria-label="{edge === 'L' ? 'Left' : 'Right'} edge sampler"
	oncontextmenu={(ev) => {
		ev.preventDefault();
		onrename();
	}}
>
	<i class="tick"></i>
	<div class="hin">
		<div class="hrow">
			<span class="setname">{head.name}</span><span class="pg">{head.page}</span>
			<button
				type="button"
				class="arrow"
				aria-label="Previous set"
				onclick={() => flipSet(edge, -1)}
				><svg viewBox="0 0 16 16" aria-hidden="true"
					><path d="M12 3 L12 13 L4 8 Z" fill="currentColor" /></svg
				></button
			>
			<button type="button" class="arrow" aria-label="Next set" onclick={() => flipSet(edge, 1)}
				><svg viewBox="0 0 16 16" aria-hidden="true"
					><path d={PLAY_TRIANGLE_PATH} fill="currentColor" /></svg
				></button
			>
		</div>
		<input
			type="search"
			aria-label="Search samples"
			placeholder="Search samples: title, key, bars"
			value={st.query}
			oninput={(ev) => setEdgeSearch(edge, ev.currentTarget.value)}
			onkeydown={onKey}
			onfocus={() => {
				st.searchFocus = true;
				sampler.lastEdge = edge;
			}}
			onblur={() => (st.searchFocus = false)}
		/>
		<div class="hrow filt">
			{#each ICON_FILTERS as f (f)}
				<button
					type="button"
					class="ico"
					aria-pressed={st.filters[f]}
					aria-label={LIBRARY_FILTER_LABELS[f].tip}
					title={LIBRARY_FILTER_LABELS[f].tip}
					onclick={() => setEdgeFilter(edge, f, !st.filters[f])}
					><svg viewBox="0 0 16 16" aria-hidden="true"><path d={ICON_PATHS[f]} /></svg></button
				>
			{/each}
			<label class="compat" title={LIBRARY_FILTER_LABELS.compatible.tip}>
				<input
					type="checkbox"
					checked={st.filters.compatible}
					onchange={(ev) => setEdgeFilter(edge, 'compatible', ev.currentTarget.checked)}
				/>
				{LIBRARY_FILTER_LABELS.compatible.name}
			</label>
			<button
				type="button"
				class="caret"
				aria-label="Compatible settings"
				aria-expanded={compatOpen}
				onpointerdown={(ev) => ev.stopPropagation()}
				onclick={oncompat}
				><svg viewBox="0 0 16 16" aria-hidden="true"
					><path d="M3 5 L13 5 L8 12 Z" fill="currentColor" /></svg
				></button
			>
		</div>
		<div class="counts">
			{#if counts !== null}
				<span>{countsLine(counts.here, counts.all)}</span>
				{#if st.results === null && counts.all > counts.here}
					<button
						type="button"
						onmousedown={(ev) => ev.preventDefault()}
						onclick={() => showEdgeResults(edge)}>show all</button
					>
				{:else if st.results !== null}
					<button
						type="button"
						onmousedown={(ev) => ev.preventDefault()}
						onclick={() => clearEdgeResults(edge)}>back to set</button
					>
				{/if}
			{:else if st.results === null}
				<span>{head.description || 'Right-click the set name to name it'}</span>
			{/if}
		</div>
	</div>
</div>

<style>
	.whead {
		top: 0;
		background: var(--es-head-bg);
		font: 13px/1.2 var(--es-font-body);
		color: var(--es-fg);
	}
	.tick {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 7px;
		background: repeating-linear-gradient(to bottom, var(--es-muted) 0 2px, transparent 2px 6px);
	}
	.whead.R .tick {
		right: 0;
	}
	.whead.L .tick {
		left: 0;
	}
	.whead.rest .tick {
		width: 3px;
		opacity: 0.35;
	}
	.hin {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 404px;
		display: grid;
		gap: 7px;
		align-content: center;
	}
	.whead.R .hin {
		right: 14px;
	}
	.whead.L .hin {
		left: 14px;
	}
	.hrow {
		display: flex;
		align-items: center;
		gap: 7px;
		min-width: 0;
	}
	.setname {
		font: 18px var(--es-font-display);
		letter-spacing: 0.6px;
		text-transform: uppercase;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		flex: 1;
		min-width: 0;
	}
	.pg,
	.counts {
		font: 12px var(--es-font-mono);
		color: var(--es-muted);
		white-space: nowrap;
	}
	.arrow {
		font-size: 12px;
		background: var(--es-raised);
		color: var(--es-fg);
		border: 1px solid var(--es-line);
		border-radius: 4px;
		width: 28px;
		height: 24px;
		padding: 0;
		cursor: pointer;
	}
	.arrow svg,
	.caret svg {
		width: 10px;
		height: 10px;
		vertical-align: middle;
	}
	input[type='search'] {
		width: 100%;
		height: 30px;
		background: var(--es-search-bg);
		border: 1px solid var(--es-tab-border);
		color: var(--es-fg);
		border-radius: 4px;
		padding: 0 9px;
		font: 13px var(--es-font-body);
		box-sizing: border-box;
	}
	.ico {
		width: 30px;
		height: 26px;
		display: grid;
		place-items: center;
		background: var(--es-raised);
		border: 1px solid var(--es-line);
		border-radius: 4px;
		color: var(--es-muted);
		cursor: pointer;
		padding: 0;
		flex: none;
	}
	.ico svg {
		width: 16px;
		height: 16px;
		stroke: currentColor;
		fill: none;
		stroke-width: 1.6;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
	.ico[aria-pressed='true'] {
		color: var(--es-accent-ink);
		background: var(--es-accent);
		border-color: var(--es-accent);
	}
	.compat {
		display: flex;
		align-items: center;
		gap: 5px;
		font-size: 13px;
		color: var(--es-fg);
		margin-left: auto;
		white-space: nowrap;
	}
	.compat input {
		accent-color: var(--es-accent);
		margin: 0;
		width: 14px;
		height: 14px;
	}
	.caret {
		background: transparent;
		border: 1px solid var(--es-line);
		color: var(--es-muted);
		border-radius: 4px;
		font-size: 11px;
		height: 26px;
		padding: 0 7px;
		cursor: pointer;
		flex: none;
	}
	.counts {
		display: flex;
		gap: 7px;
		align-items: center;
		overflow: hidden;
		text-overflow: ellipsis;
		min-height: 16px;
	}
	.counts button {
		font: inherit;
		color: var(--es-deck-a);
		background: none;
		border: 0;
		padding: 0;
		cursor: pointer;
		text-decoration: underline;
	}
</style>
