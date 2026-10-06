<script lang="ts">
	// Dev mockup: production LyricsLane / WordLane over a production blocks
	// wave row at deck zoom, for one real track each, without touching the
	// live /performance session (a second /performance client competes for
	// the command bus).
	// /mockups/lyric-rows?sid=<lines track>&wsid=<words track>&t=<s>&wt=<s>&h=<row px>&skin=mono-dev
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { fetchAnlz, fetchTrackBypassingHttpCache, fetchTrackLyrics } from '$lib/rb/api-rb';
	import { getTrackLyricsWords, type KaraokeWord } from '$lib/api-karaoke';
	import { drawWaveRow, readPalette } from '$lib/components/rb/wave/render';
	import LyricsLane from '$lib/components/rb/wave/LyricsLane.svelte';
	import WordLane from '$lib/components/rb/wave/WordLane.svelte';
	import type { LyricLine } from '$lib/components/rb/wave/lyrics-lane';
	import '$lib/rb/theme.css';

	const WIDTH = 1148; // measured main wave row width at a 1280px viewport
	const params = page.url.searchParams;
	const sid = params.get('sid');
	const wsid = params.get('wsid');
	const tS = Number(params.get('t') ?? '60');
	const wtS = Number(params.get('wt') ?? '30');
	const rowH = Number(params.get('h') ?? '56');
	const skin = params.get('skin');

	let root: HTMLDivElement;
	let lineCanvas: HTMLCanvasElement | undefined = $state();
	let wordCanvas: HTMLCanvasElement | undefined = $state();
	let lines: { lines: LyricLine[] } | null = $state(null);
	let words: KaraokeWord[] | null = $state(null);
	let error: string | null = $state(null);

	async function _paint(el: HTMLCanvasElement, stableId: string, atS: number): Promise<void> {
		const [anlz, track] = await Promise.all([fetchAnlz(stableId), fetchTrackBypassingHttpCache(stableId)]);
		if (track.duration_ms === null || track.duration_ms === undefined) throw new Error(`${stableId} has no duration_ms`);
		const dpr = window.devicePixelRatio || 1;
		el.width = WIDTH * dpr;
		el.height = rowH * dpr;
		const ctx = el.getContext('2d');
		if (ctx === null) throw new Error('2d context unavailable');
		ctx.scale(dpr, dpr);
		drawWaveRow(ctx, {
			widthCss: WIDTH,
			heightCss: rowH,
			positionMs: atS * 1000,
			durationMs: track.duration_ms,
			anlz,
			palette: readPalette(root),
			pitch: 1,
			loop: null,
			waveformDesign: 'blocks'
		});
	}

	onMount(async () => {
		if (skin !== null) {
			document.documentElement.dataset.skin = skin;
			document.documentElement.dataset.wavePalette = 'mono';
		}
		if (sid === null) {
			error = 'missing ?sid=<stable_id with cached lyric lines>';
			return;
		}
		lines = await fetchTrackLyrics(sid);
		if (lines === null) throw new Error(`no cached lyric lines for ${sid}`);
		if (lineCanvas === undefined) throw new Error('line canvas not mounted');
		await _paint(lineCanvas, sid, tS);
		if (wsid !== null) {
			const karaoke = await getTrackLyricsWords(wsid);
			if (karaoke === null) throw new Error(`no karaoke words for ${wsid}`);
			words = karaoke.words;
			if (wordCanvas === undefined) throw new Error('word canvas not mounted');
			await _paint(wordCanvas, wsid, wtS);
		}
	});
</script>

<div class="perf-root mock" bind:this={root}>
	<h1>lyric rows (line lane {sid} @ {tS}s, word lane {wsid ?? 'none'} @ {wtS}s, row {rowH}px)</h1>
	{#if error !== null}<p class="err">{error}</p>{/if}
	<div class="row" style="width:{WIDTH}px;height:{rowH}px" data-lane="lines">
		<canvas bind:this={lineCanvas} style="width:{WIDTH}px;height:{rowH}px"></canvas>
		<LyricsLane lyrics={lines} loadError={null} positionMs={tS * 1000} pitch={1} />
	</div>
	{#if wsid !== null}
		<div class="row" style="width:{WIDTH}px;height:{rowH}px" data-lane="words">
			<canvas bind:this={wordCanvas} style="width:{WIDTH}px;height:{rowH}px"></canvas>
			{#if words !== null}<WordLane {words} positionMs={wtS * 1000} pitch={1} />{/if}
		</div>
	{/if}
</div>

<style>
	.mock {
		min-height: 100vh;
		padding: 12px 16px;
	}
	h1 {
		font-size: 12px;
		font-weight: 500;
		margin: 0 0 10px;
	}
	.row {
		position: relative;
		margin-bottom: 10px;
		border: 1px solid var(--rb-border);
	}
	canvas {
		display: block;
	}
	.err {
		color: var(--rb-red);
	}
</style>
