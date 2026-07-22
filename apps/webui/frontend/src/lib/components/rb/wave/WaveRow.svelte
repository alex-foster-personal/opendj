<script lang="ts">
	// Build unit: wavestack - ONE deck row (COMPONENT-MAP 1.2, SCREENSHOT-SPEC 2).
	// Canvas window scrolls under a fixed center playhead; left gutter shows
	// the deck number + bars-to-next-cue counter. Empty deck = flat dark row.
	// rAF repaints ONLY while this deck is playing or being scrubbed.
	import { vocalsOf } from '$lib/rb/api-rb';
	import {
		performanceCommandStatus,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';
	import type { DeckId } from '$lib/rb/types';
	import { getDeckState } from './engine-accessor';
	import { ensureAnlz, getAnlzEntry } from './anlz-cache.svelte';
	import { ensureBeatgridFallback, getBeatgridFallbackEntry } from './beatgrid-fallback-cache.svelte';
	import { shouldUseBeatgridFallback, toSyntheticAnlzData } from '$lib/rb/beatgrid-fallback';
	import { barsToNextCueLabel } from './wave-math';
	import { drawWaveRow, readPalette, WAVE_WINDOW_S, type WavePalette } from './render';
	import {
		createLatestSeekDispatcher,
		waveClickTargetMs,
		waveDragTargetMs
	} from './wave-scrub';

	const { deckId }: { deckId: DeckId } = $props();

	const deck = $derived(getDeckState(deckId));
	const commandPending = $derived(performanceCommandStatus.deck_pending[deckId] > 0);

	// ---- anlz source: prefer the engine-populated payload; else our own
	// cached /anlz fetch keyed by the deck's stable_id (deck-load event).
	$effect(() => {
		const sid = deck.stable_id;
		if (sid !== null && deck.anlz === null && deck.anlz_error === null) ensureAnlz(sid);
	});
	const anlzData = $derived.by(() => {
		if (deck.anlz !== null) return deck.anlz;
		if (deck.stable_id === null) return null;
		const entry = getAnlzEntry(deck.stable_id);
		return entry !== undefined && entry.status === 'ready' ? entry.data : null;
	});
	const anlzErrorCode = $derived.by(() => {
		if (deck.anlz_error !== null) return deck.anlz_error;
		if (deck.stable_id === null) return null;
		const entry = getAnlzEntry(deck.stable_id);
		return entry !== undefined && entry.status === 'error' ? entry.code : null;
	});

	// ---- beatgrid fallback: only reached once /anlz has confirmed no
	// rekordbox ANLZ exists (anlz-fallback-beatgrid, LANE analysis-router).
	// ANLZ always preferred - this never races or overrides a real payload.
	$effect(() => {
		const sid = deck.stable_id;
		if (sid !== null && shouldUseBeatgridFallback(anlzErrorCode)) ensureBeatgridFallback(sid);
	});
	const beatgridFallback = $derived.by(() => {
		if (deck.stable_id === null || !shouldUseBeatgridFallback(anlzErrorCode)) return null;
		const entry = getBeatgridFallbackEntry(deck.stable_id);
		return entry !== undefined && entry.status === 'ready' ? entry.data : null;
	});
	// What the painter/bars-label actually consume: the real ANLZ payload
	// when present, else a synthesized beatgrid-only payload, else null.
	const paintAnlz = $derived(
		anlzData ?? (beatgridFallback !== null ? toSyntheticAnlzData(beatgridFallback) : null)
	);

	// Bars until next cue; null (hidden) without a beatgrid or upcoming cue.
	const barsLabel = $derived(
		paintAnlz !== null ? barsToNextCueLabel(paintAnlz, deck.position_ms) : null
	);

	// Vocal state tooltip (SPIKE-B1/B2 four mandatory states): bars are
	// painted by render.ts for 'rekordbox' and 'demucs'; the barless
	// states get an explicit tooltip so absence is never ambiguous, and
	// demucs bars declare their non-rekordbox provenance.
	const vocalsTitle = $derived.by((): string | null => {
		if (anlzData === null) return null;
		const v = vocalsOf(anlzData);
		if (v.status === 'no_vocals') return 'no vocals detected';
		else if (v.status === 'not_analyzed') return 'vocals not analyzed in rekordbox';
		else if (v.status === 'demucs') return 'vocals: local detection';
		else return null; // rekordbox: the blue bars speak for themselves
	});

	// ---- canvas plumbing
	let canvasEl: HTMLCanvasElement | undefined = $state();
	let cssW = $state(0);
	let cssH = $state(0);
	let palette: WavePalette | null = null;
	let seeking = $state(false);
	let scrubPointerId: number | null = null;
	let scrubOriginClientX = 0;
	let scrubOriginPositionMs = 0;
	let scrubDurationMs = 0;
	let scrubLeftPx = 0;
	let scrubWidthPx = 0;
	let scrubMoved = false;
	let lastSeekTs = 0;
	const DRAG_THRESHOLD_PX = 3;
	const seekDispatcher = createLatestSeekDispatcher(async (positionMs) => {
		await runPerformanceCommandFromUi({ type: 'seek', deck: deckId, position_ms: positionMs });
	});

	$effect(() => {
		const el = canvasEl;
		if (el === undefined) return;
		palette = readPalette(el); // throws if not under .perf-root
		const observer = new ResizeObserver((entries) => {
			const rect = entries[0].contentRect;
			cssW = Math.round(rect.width);
			cssH = Math.round(rect.height);
		});
		observer.observe(el);
		return () => observer.disconnect();
	});

	function draw(): void {
		const el = canvasEl;
		if (el === undefined || palette === null || cssW === 0 || cssH === 0) return;
		const dpr = window.devicePixelRatio;
		if (el.width !== cssW * dpr || el.height !== cssH * dpr) {
			el.width = cssW * dpr;
			el.height = cssH * dpr;
		}
		const ctx = el.getContext('2d');
		if (ctx === null) throw new Error(`wavestack deck ${deckId}: 2d context unavailable`);
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		if (deck.stable_id === null || deck.duration_ms === null) {
			// Empty deck: flat dark row - never an invented waveform.
			ctx.fillStyle = palette.bg;
			ctx.fillRect(0, 0, cssW, cssH);
			return;
		}
		drawWaveRow(ctx, {
			widthCss: cssW,
			heightCss: cssH,
			positionMs: deck.position_ms,
			durationMs: deck.duration_ms,
			anlz: paintAnlz,
			palette
		});
	}

	// rAF loop ONLY while playing or scrubbing (task requirement).
	$effect(() => {
		if (!(deck.playing || seeking)) return;
		let raf = requestAnimationFrame(function loop() {
			draw();
			raf = requestAnimationFrame(loop);
		});
		return () => cancelAnimationFrame(raf);
	});

	// Static repaint on load/seek/resize/anlz-arrival while stopped. The
	// early return keeps position_ms untracked during playback so this
	// effect stays quiet while the rAF loop owns the canvas.
	$effect(() => {
		if (deck.playing || seeking) return;
		void deck.stable_id;
		void deck.position_ms;
		void paintAnlz;
		void anlzErrorCode;
		void cssW;
		void cssH;
		draw();
	});

	// ---- click-drag seek: the engine position at pointerdown is frozen as
	// the gesture origin. Dragging grabs the waveform under the fixed playhead;
	// click-without-drag still seeks to the time visibly beneath the pointer.
	function _dragTarget(clientX: number): number {
		return waveDragTargetMs({
			originPositionMs: scrubOriginPositionMs,
			originClientX: scrubOriginClientX,
			clientX,
			widthPx: scrubWidthPx,
			durationMs: scrubDurationMs,
			windowSeconds: WAVE_WINDOW_S
		});
	}

	function _clickTarget(clientX: number): number {
		return waveClickTargetMs({
			centerPositionMs: scrubOriginPositionMs,
			pointerX: clientX - scrubLeftPx,
			widthPx: scrubWidthPx,
			durationMs: scrubDurationMs,
			windowSeconds: WAVE_WINDOW_S
		});
	}

	function _clearGesture(): void {
		scrubPointerId = null;
		scrubMoved = false;
		seeking = false;
	}

	async function onPointerDown(event: PointerEvent): Promise<void> {
		// Empty deck rows are inert - a real state, nothing to seek.
		if (
			!event.isPrimary ||
			event.button !== 0 ||
			deck.stable_id === null ||
			deck.duration_ms === null ||
			commandPending
		) {
			return;
		}
		event.preventDefault();
		const canvas = event.currentTarget as HTMLCanvasElement;
		const rect = canvas.getBoundingClientRect();
		canvas.setPointerCapture(event.pointerId);
		scrubPointerId = event.pointerId;
		scrubOriginClientX = event.clientX;
		scrubOriginPositionMs = deck.position_ms;
		scrubDurationMs = deck.duration_ms;
		scrubLeftPx = rect.left;
		scrubWidthPx = rect.width;
		scrubMoved = false;
		seeking = true;
		lastSeekTs = performance.now();
	}

	async function onPointerMove(event: PointerEvent): Promise<void> {
		if (!seeking || event.pointerId !== scrubPointerId) return;
		const deltaPx = event.clientX - scrubOriginClientX;
		if (!scrubMoved && Math.abs(deltaPx) < DRAG_THRESHOLD_PX) return;
		scrubMoved = true;
		const now = performance.now();
		if (now - lastSeekTs < 90) return; // throttle buffer-source restarts
		lastSeekTs = now;
		await seekDispatcher.request(_dragTarget(event.clientX));
	}

	async function onPointerUp(event: PointerEvent): Promise<void> {
		if (!seeking || event.pointerId !== scrubPointerId) return;
		const canvas = event.currentTarget as HTMLCanvasElement;
		try {
			const targetMs = scrubMoved ? _dragTarget(event.clientX) : _clickTarget(event.clientX);
			await seekDispatcher.request(targetMs);
		} finally {
			_clearGesture();
			if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
		}
	}

	function onPointerCancel(event: PointerEvent): void {
		if (!seeking || event.pointerId !== scrubPointerId) return;
		const canvas = event.currentTarget as HTMLCanvasElement;
		_clearGesture();
		if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
	}

	function onLostPointerCapture(event: PointerEvent): void {
		if (seeking && event.pointerId === scrubPointerId) _clearGesture();
	}
</script>

<div class="rb-waverow">
	<div class="gutter">
		<span class="deck-num">{deckId}</span>
		{#if barsLabel !== null}<span class="bars">{barsLabel}</span>{/if}
	</div>
	<div class="canvas-wrap" title={vocalsTitle ?? undefined}>
		<canvas
			bind:this={canvasEl}
			role="slider"
			aria-label="deck {deckId} waveform seek"
			aria-valuemin={0}
			aria-valuemax={deck.duration_ms ?? 0}
			aria-valuenow={Math.round(deck.position_ms)}
			aria-disabled={deck.stable_id === null || (commandPending && !seeking)}
			tabindex="-1"
			onpointerdown={onPointerDown}
			onpointermove={onPointerMove}
			onpointerup={onPointerUp}
			onpointercancel={onPointerCancel}
			onlostpointercapture={onLostPointerCapture}
		></canvas>
		{#if deck.stable_id !== null && anlzErrorCode !== null && beatgridFallback === null}
			<span class="anlz-state" title={anlzErrorCode}>
				{anlzErrorCode === 'ANALYSIS_NOT_FOUND' ? 'NO ANALYSIS' : `ANLZ ERROR ${anlzErrorCode}`}
			</span>
		{:else if beatgridFallback !== null}
			<span
				class="anlz-state"
				title="no rekordbox ANLZ - beatgrid from apps.analysis (fallback, never invented)"
			>
				BPM {beatgridFallback.bpm.toFixed(1)} (fallback)
			</span>
		{/if}
	</div>
</div>

<style>
	.rb-waverow {
		display: flex;
		height: var(--rb-waverow-h);
		background: var(--rb-bg);
		border-bottom: 1px solid var(--rb-border);
	}
	.gutter {
		width: 56px;
		flex: none;
		display: flex;
		flex-direction: column;
		justify-content: center;
		gap: 1px;
		padding: 0 4px 0 6px;
		border-right: 1px solid var(--rb-border);
	}
	.deck-num {
		color: var(--rb-text);
		font-size: var(--rb-fs-deck-title);
		font-weight: 600;
		line-height: 1.1;
	}
	.bars {
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		white-space: nowrap;
	}
	.canvas-wrap {
		position: relative;
		flex: 1;
		min-width: 0;
	}
	canvas {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		display: block;
		cursor: ew-resize;
		outline: none;
		touch-action: none;
	}
	.anlz-state {
		position: absolute;
		left: 50%;
		top: 50%;
		transform: translate(-50%, -50%);
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		letter-spacing: 1px;
		pointer-events: none;
	}
</style>
