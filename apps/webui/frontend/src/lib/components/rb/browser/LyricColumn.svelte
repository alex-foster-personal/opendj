<script lang="ts">
	/**
	 * Library lyrics column cell: glyph, sync percent, hover tip, and
	 * lyrics-cache load strategy. Extracted from TrackTable so the table
	 * imports one child instead of owning lyric hover plumbing.
	 */
	import type { BrowserRow } from './pane-contract.svelte';
	import LyricTip from './LyricTip.svelte';
	import {
		LYRIC_NO_DATA_TITLE,
		LYRIC_QUALITY_TITLE,
		lyricSyncQualityPct,
		lyricVerdictMark,
		lyricVerdictTitle
	} from './lyric-column';
	import LyricVerdictMark from './LyricVerdictMark.svelte';
	import {
		cancelHoverLoad,
		hoverLoadLyrics,
		loadLyrics,
		lyricEntry
	} from '$lib/lyrics/lyrics-cache.svelte';
	import { uiPrefs } from '$lib/rb/prefs.svelte';

	let { row }: { row: BrowserRow } = $props();

	let tipOpen = $state(false);
	let anchor = $state<{ x: number; top: number; bottom: number } | null>(null);
	let _closeTimer: ReturnType<typeof setTimeout> | null = null;
	let _openTimer: ReturnType<typeof setTimeout> | null = null;

	function _cancelClose(): void {
		if (_closeTimer !== null) {
			clearTimeout(_closeTimer);
			_closeTimer = null;
		}
	}

	function _scheduleClose(): void {
		_cancelClose();
		_closeTimer = setTimeout(() => {
			_closeTimer = null;
			tipOpen = false;
			anchor = null;
		}, 140);
	}

	function onCellEnter(event: PointerEvent): void {
		_cancelClose();
		if (_openTimer !== null) clearTimeout(_openTimer);
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		const nextAnchor = { x: rect.left, top: rect.top, bottom: rect.bottom };
		if (tipOpen) {
			anchor = nextAnchor;
		} else {
			_openTimer = setTimeout(() => {
				_openTimer = null;
				anchor = nextAnchor;
				tipOpen = true;
			}, 200);
		}
		const strategy = uiPrefs.lyrics_load_strategy;
		if (strategy === 'hover') {
			hoverLoadLyrics(row.stable_id);
		} else if (
			strategy === 'in-view' &&
			row.lyrics?.has_words === true &&
			lyricEntry(row.stable_id) === null
		) {
			void loadLyrics(row.stable_id);
		}
	}

	function onCellLeave(): void {
		cancelHoverLoad(row.stable_id);
		if (_openTimer !== null) {
			clearTimeout(_openTimer);
			_openTimer = null;
		}
		_scheduleClose();
	}
</script>

<td class="c-lyrics" data-custom-tip="" onpointerenter={onCellEnter} onpointerleave={onCellLeave}>
	{#if row.lyrics === null}
		<span class="lyr-dash" title={LYRIC_NO_DATA_TITLE}>-</span>
	{:else}
		{@const pct = lyricSyncQualityPct(row.lyrics.pct_witness_red)}
		<span class="lyr-glyph" title={lyricVerdictTitle(row.lyrics)}
			><LyricVerdictMark mark={lyricVerdictMark(row.lyrics.effective)} /></span
		>
		{#if pct !== null}
			<span class="lyr-pct" title={LYRIC_QUALITY_TITLE}>{pct}%</span>
		{/if}
	{/if}
</td>

{#if tipOpen && anchor !== null}
	<LyricTip
		stableId={row.stable_id}
		summary={row.lyrics}
		{anchor}
		onpointerenter={_cancelClose}
		onpointerleave={_scheduleClose}
	/>
{/if}

<style>
	.c-lyrics {
		text-align: center;
		white-space: nowrap;
		font-size: 11px;
	}
	.lyr-dash {
		color: var(--rb-text-dim, #7a8088);
	}
	.lyr-glyph {
		font-weight: 700;
		margin-right: 4px;
	}
	.lyr-pct {
		color: var(--rb-text-dim, #9aa3ad);
		font-variant-numeric: tabular-nums;
	}
</style>
