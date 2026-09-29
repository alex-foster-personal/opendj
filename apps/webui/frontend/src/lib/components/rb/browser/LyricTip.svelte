<script lang="ts">
	/**
	 * Rich lyric hover panel for the library lyrics column (ConnDotsStrip
	 * .conn-panel precedent - a native title cannot hold multi-line lyric
	 * text). Reads the shared lyric cache reactively; it NEVER fetches
	 * itself - the cell's pointerenter handler in TrackTable owns the
	 * load-strategy decision (hover debounce / in-view catch-up / off).
	 */
	import type { LyricsRowSummary } from '$lib/rb/lyrics/types';
	import { lyricEntry } from '$lib/lyrics/lyrics-cache.svelte';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import {
		groupLinesIntoParagraphs,
		LYRIC_QUALITY_TITLE,
		lyricSyncQualityPct,
		lyricVerdictMark,
		lyricVerdictTitle
	} from './lyric-column';
	import LyricVerdictMark from './LyricVerdictMark.svelte';
	import { placeFloating } from '$lib/ui/clamp-to-viewport';

	let {
		stableId,
		summary,
		anchor,
		onpointerenter,
		onpointerleave
	}: {
		stableId: string;
		/** Row summary from the listing hydrate; null = pipeline never ran. */
		summary: LyricsRowSummary | null;
		/** Viewport-space rect of the hovered cell (getBoundingClientRect). */
		anchor: { x: number; top: number; bottom: number };
		/** Keep-open plumbing: TrackTable cancels/schedules its close timer
		 * so the panel survives the pointer crossing cell -> panel (needed
		 * to scroll long lyrics). */
		onpointerenter?: () => void;
		onpointerleave?: () => void;
	} = $props();

	const entry = $derived(lyricEntry(stableId));
	const strategy = $derived(uiPrefs.lyrics_load_strategy);
	const qualityPct = $derived(
		summary === null ? null : lyricSyncQualityPct(summary.pct_witness_red)
	);
	const paragraphs = $derived.by(() => {
		if (entry === null || entry.state !== 'loaded' || entry.track === null) return null;
		const lines = entry.track.lines;
		return lines == null ? null : groupLinesIntoParagraphs(lines);
	});

	// ------------------------------------------- viewport-clamped position
	let el = $state<HTMLDivElement | null>(null);
	let panelW = $state(0);
	let panelH = $state(0);
	let winW = $state(0);
	let winH = $state(0);

	$effect(() => {
		const node = el;
		if (node === null || typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver(() => {
			panelW = node.offsetWidth;
			panelH = node.offsetHeight;
		});
		ro.observe(node);
		return () => ro.disconnect();
	});

	const pos = $derived.by(() => {
		if (panelW <= 0 || panelH <= 0) {
			return { left: anchor.x, top: anchor.bottom + 4 };
		}
		const box = placeFloating({
			trigger: {
				left: anchor.x,
				top: anchor.top,
				width: 0,
				height: anchor.bottom - anchor.top
			},
			size: { width: panelW, height: panelH },
			viewport: { width: winW, height: winH },
			preferred: 'below',
			gap: 4
		});
		return { left: box.x, top: box.y };
	});
</script>

<svelte:window bind:innerWidth={winW} bind:innerHeight={winH} />

<div
	class="lyric-tip"
	bind:this={el}
	style={`left:${pos.left}px;top:${pos.top}px`}
	role="tooltip"
	onpointerenter={() => onpointerenter?.()}
	onpointerleave={() => onpointerleave?.()}
>
	{#if summary === null}
		<p class="lt-state">no lyric data yet - pipeline has not processed this track</p>
	{:else}
		<div class="lt-head">
			<span class="lt-verdict" title={lyricVerdictTitle(summary)}>
				<LyricVerdictMark mark={lyricVerdictMark(summary.effective)} />
				{summary.effective}{summary.override !== null ? ' (override)' : ''}
			</span>
		</div>
		{#if !summary.has_words}
			<p class="lt-state">
				no word-level alignment for this track - the verdict comes from stem vocal coverage alone
			</p>
		{:else if entry !== null && entry.state === 'loaded'}
			{#if paragraphs === null}
				<p class="lt-state lt-error">
					payload has words but no derived lines - server include=lines contract not met
				</p>
			{:else}
				<div class="lt-body">
					{#each paragraphs as para, pi (pi)}
						{#if pi > 0}<div class="lt-para-gap" aria-hidden="true"></div>{/if}
						{#each para as line (line.first_idx)}
							<div class="lt-line">{line.text}</div>
						{/each}
					{/each}
				</div>
			{/if}
		{:else if strategy === 'off'}
			<p class="lt-state">lyric loading is off in settings</p>
		{:else if entry === null || entry.state === 'loading'}
			<p class="lt-state">loading lyrics...</p>
		{:else if entry.state === 'none'}
			<p class="lt-state">no lyric payload on the server for this track</p>
		{:else}
			<p class="lt-state lt-error">lyrics fetch failed: {entry.error}</p>
		{/if}
		<div class="lt-foot">
			<span title="lyric source provider the pipeline aligned against">
				{summary.source ?? 'unknown source'}
			</span>
			<span title="detected lyric language (ISO 639-3)">
				{summary.language_iso3 ?? 'lang ?'}
			</span>
			{#if qualityPct !== null}
				<span title={LYRIC_QUALITY_TITLE}>{qualityPct}% sync</span>
			{/if}
			{#if summary.n_words !== null}
				<span title="aligned word count from the pipeline">{summary.n_words} words</span>
			{/if}
		</div>
	{/if}
</div>

<style>
	.lyric-tip {
		position: fixed;
		z-index: 90;
		display: flex;
		flex-direction: column;
		width: min(360px, 80vw);
		max-height: min(70vh, 560px);
		padding: 8px 10px;
		border: 1px solid var(--rb-border, #2e333a);
		border-radius: 4px;
		background: color-mix(in srgb, var(--rb-panel, #1a1e24) 94%, #000);
		color: var(--rb-text, #d7dde5);
		font-size: 11px;
		line-height: 1.35;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
		pointer-events: auto;
	}
	.lt-head {
		flex: none;
		margin-bottom: 4px;
		font-weight: 700;
	}
	.lt-verdict {
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-size: 10px;
	}
	.lt-state {
		margin: 0;
		color: var(--rb-text-dim, #9aa3ad);
	}
	.lt-error {
		color: #e07070;
	}
	.lt-body {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		margin: 2px 0;
		padding-right: 4px;
	}
	.lt-line {
		white-space: pre-wrap;
	}
	.lt-para-gap {
		height: 0.9em;
	}
	.lt-foot {
		flex: none;
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-top: 6px;
		padding-top: 4px;
		border-top: 1px solid var(--rb-border, #2e333a);
		color: var(--rb-text-dim, #9aa3ad);
		font-size: 10px;
	}
</style>
