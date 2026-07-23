<script lang="ts">
	// Strip overview waveform (~20px, SCREENSHOT-SPEC 3): native 400-point
	// PWAV preview bands from /anlz, position marker from the engine clock,
	// cue letters + memory markers, loop in/out time chips, click-to-seek.
	// No analysis -> explicit 'NO ANALYSIS' state; empty deck -> blank strip.
	// Never synthesised waveforms (COMPONENT-MAP 1.3).
	// Vocal blue bars (SPIKE-B1): 2px-ish top layer, drawn ONLY for real
	// PVDI regions (status 'rekordbox'); the two barless states surface as
	// explicit tooltips - three mandatory states, nothing invented.
	import { vocalsOf, type Vocals } from '$lib/rb/api-rb';
	import type { AnlzWaveformBands, DeckState, HotCueSlot } from '$lib/rb/types';
	import { VOCAL_BLUE, vocalAlpha } from '../wave/render';

	let {
		deck,
		pending,
		onSeek,
		onPlay
	}: {
		deck: DeckState;
		pending: boolean;
		onSeek: (ms: number) => Promise<void>;
		onPlay: () => Promise<void>;
	} = $props();

	// Canvas backing resolution (CSS scales to 100% x var(--rb-strip-h)).
	const W = 400;
	const H = 40;

	let canvas: HTMLCanvasElement | undefined = $state();
	/** Play affordance above the last paused seek point (pct along strip). */
	let playHintPct: number | null = $state(null);

	const posPct: number = $derived(
		deck.duration_ms === null || deck.duration_ms === 0
			? 0
			: (deck.position_ms / deck.duration_ms) * 100
	);

	// Validated vocals of the loaded analysis; null while no anlz payload.
	const vocals: Vocals | null = $derived(deck.anlz === null ? null : vocalsOf(deck.anlz));

	const vocalsTitle: string | null = $derived.by(() => {
		if (vocals === null) return null;
		if (vocals.status === 'no_vocals') return 'no vocals detected';
		else if (vocals.status === 'not_analyzed') return 'vocals not analyzed in rekordbox';
		else if (vocals.status === 'demucs') return 'vocals: local detection';
		else return null; // rekordbox: the bars themselves are the signal
	});

	const memoryCuesMs: number[] = $derived(
		deck.anlz === null
			? []
			: deck.anlz.cues.filter((c) => c.kind === 'memory').map((c) => c.in_ms)
	);

	// First stored loop in the hot-cue bank -> in/out time chips (display-only
	// at v1 per COMPONENT-MAP 1.3; sparse coverage is real).
	const loopCue: { slot: HotCueSlot; in_ms: number; out_ms: number } | null = $derived.by(() => {
		for (const hc of deck.hot_cues) {
			if (hc.is_loop && hc.out_ms !== null) {
				return { slot: hc.slot, in_ms: hc.in_ms, out_ms: hc.out_ms };
			}
		}
		return null;
	});

	// ----------------------------------------------------------- _helpers

	function _pctOf(ms: number): number {
		if (deck.duration_ms === null || deck.duration_ms === 0) return 0;
		return (ms / deck.duration_ms) * 100;
	}

	function _fmtMmSs(ms: number): string {
		const totalS = Math.max(0, Math.floor(ms / 1000));
		const m = Math.floor(totalS / 60);
		const s = totalS % 60;
		return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}

	function _bar(ctx: CanvasRenderingContext2D, x: number, w: number, v: number, colour: string): void {
		const h = Math.max(0, Math.min(1, v)) * H;
		ctx.fillStyle = colour;
		ctx.fillRect(x, H - h, w, h);
	}

	function _drawPreview(ctx: CanvasRenderingContext2D, bands: AnlzWaveformBands, kind: 'tri' | 'mono'): void {
		ctx.clearRect(0, 0, W, H);
		const n = bands.length;
		if (n === 0) return;
		const w = W / n;
		for (let i = 0; i < n; i++) {
			const x = i * w;
			if (kind === 'tri') {
				// Palette per SCREENSHOT-SPEC 6: lows orange, mids blue, highs white.
				_bar(ctx, x, w, bands.low[i], '#e8a13a');
				_bar(ctx, x, w, bands.mid[i], 'rgba(61, 125, 217, 0.85)');
				_bar(ctx, x, w, bands.high[i], 'rgba(207, 224, 242, 0.9)');
			} else {
				// mono = heights only; single colour, never synthesised bands.
				const v = Math.max(bands.low[i], bands.mid[i], bands.high[i]);
				_bar(ctx, x, w, v, '#3d7dd9');
			}
		}
	}

	function _drawVocalBars(ctx: CanvasRenderingContext2D, v: Vocals): void {
		// rekordbox + demucs render identically; barless states draw nothing
		if (v.status !== 'rekordbox' && v.status !== 'demucs') return;
		const dur = deck.duration_ms;
		if (dur === null || dur === 0) return; // cannot place bars without a duration
		ctx.fillStyle = VOCAL_BLUE;
		for (const region of v.regions) {
			const x0 = Math.max(0, ((region.start_s * 1000) / dur) * W);
			const x1 = Math.min(W, ((region.end_s * 1000) / dur) * W);
			if (x1 <= x0) continue;
			ctx.globalAlpha = vocalAlpha(region.intensity);
			// 4 canvas px on the 40px backing = ~2 CSS px at --rb-strip-h 20px.
			ctx.fillRect(x0, 0, x1 - x0, 4);
		}
		ctx.globalAlpha = 1;
	}

	$effect(() => {
		const c = canvas;
		if (c === undefined) return;
		const ctx = c.getContext('2d');
		if (ctx === null) throw new Error('StripWaveform: canvas 2d context unavailable');
		if (deck.anlz === null) {
			ctx.clearRect(0, 0, W, H);
			return;
		}
		_drawPreview(ctx, deck.anlz.waveform.preview, deck.anlz.waveform.kind);
		if (vocals !== null) _drawVocalBars(ctx, vocals);
	});

	$effect(() => {
		if (deck.playing || deck.stable_id === null) playHintPct = null;
	});

	async function handleClick(e: MouseEvent): Promise<void> {
		if (deck.duration_ms === null) return;
		const el = e.currentTarget as HTMLElement;
		const rect = el.getBoundingClientRect();
		const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
		await onSeek(ratio * deck.duration_ms);
		if (!deck.playing) playHintPct = ratio * 100;
	}

	async function handlePlayHint(e: MouseEvent): Promise<void> {
		e.stopPropagation();
		playHintPct = null;
		await onPlay();
	}
</script>

<div class="strip-wrap">
	{#if playHintPct !== null && !deck.playing && deck.stable_id !== null}
		<button
			type="button"
			class="play-hint"
			style={`left:${playHintPct}%`}
			aria-label="Play from here"
			title="Play"
			onclick={(e) => void handlePlayHint(e)}
		>
			▶
		</button>
	{/if}
	<button
		class="strip"
		onclick={(e) => void handleClick(e)}
		disabled={deck.stable_id === null || pending}
		aria-label="track overview waveform - click to seek"
		title={vocalsTitle ?? undefined}
	>
		<canvas bind:this={canvas} width={W} height={H}></canvas>

		{#if deck.anlz_error !== null}
			<span class="no-anlz">NO ANALYSIS</span>
		{/if}

		{#each memoryCuesMs as ms, i (i)}
			<span class="mem-cue" style={`left:${_pctOf(ms)}%`}></span>
		{/each}

		{#each deck.hot_cues as hc (hc.slot)}
			<span class="cue-letter" style={`left:${_pctOf(hc.in_ms)}%`}>{hc.slot}</span>
		{/each}

		{#if loopCue !== null}
			<span class="loop-chip in" style={`left:${_pctOf(loopCue.in_ms)}%`}>
				{loopCue.slot} {_fmtMmSs(loopCue.in_ms)}
			</span>
			<span class="loop-chip out" style={`left:${_pctOf(loopCue.out_ms)}%`}>
				{_fmtMmSs(loopCue.out_ms)}
			</span>
		{/if}

		{#if deck.stable_id !== null}
			<span class="pos" style={`left:${posPct}%`}></span>
		{/if}
	</button>
</div>

<style>
	.strip-wrap {
		position: relative;
		width: 100%;
		flex: 0 0 auto;
		overflow: visible;
		z-index: 2;
	}
	.play-hint {
		position: absolute;
		bottom: calc(100% + 2px);
		transform: translateX(-50%);
		z-index: 5;
		width: 22px;
		height: 22px;
		padding: 0;
		border: 1px solid rgba(236, 240, 244, 0.65);
		border-radius: 50%;
		background: color-mix(in srgb, var(--rb-panel-raised) 70%, #000);
		color: #fff;
		font-size: 11px;
		line-height: 1;
		cursor: pointer;
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45);
	}
	.play-hint:hover {
		background: var(--rb-accent);
		border-color: var(--rb-accent);
	}
	.strip {
		position: relative;
		display: block;
		width: 100%;
		height: var(--rb-strip-h);
		min-height: var(--rb-strip-h);
		padding: 0;
		margin: 0;
		background: #080a0d;
		border: 1px solid var(--rb-border);
		cursor: pointer;
		overflow: hidden;
	}
	.strip:disabled {
		cursor: default;
	}
	canvas {
		display: block;
		width: 100%;
		height: 100%;
	}
	.no-anlz {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		letter-spacing: 1px;
	}
	.mem-cue {
		position: absolute;
		top: 0;
		width: 0;
		height: 0;
		margin-left: -3px;
		border-left: 3px solid transparent;
		border-right: 3px solid transparent;
		border-top: 4px solid var(--rb-red);
	}
	.cue-letter {
		position: absolute;
		top: 0;
		margin-left: -1px;
		padding: 0 1px;
		font-size: 8px;
		line-height: 9px;
		color: #fff;
		background: var(--rb-green);
		border-radius: 1px;
	}
	.loop-chip {
		position: absolute;
		bottom: 0;
		margin-left: 1px;
		padding: 0 2px;
		font-size: 8px;
		line-height: 10px;
		font-variant-numeric: tabular-nums;
		border-radius: 1px;
		white-space: nowrap;
	}
	.loop-chip.in {
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		border: 1px solid var(--rb-border);
	}
	.loop-chip.out {
		background: var(--rb-green);
		color: #04120a;
	}
	.pos {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 1px;
		background: #ffffff;
		box-shadow: 0 0 3px rgba(255, 255, 255, 0.7);
	}
</style>
