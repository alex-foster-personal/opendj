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
	import type { DeckId } from '$lib/rb/deck-slots';
	import { getDeckState, DECK_IDS, mixerState } from './engine-accessor';
	import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';
	import { deckHoverUi, setHoveredDeck } from '$lib/rb/deck-hover.svelte';
	import {
		deckAnlzNeedsFetch,
		ensureAnlz,
		getAnlzEntry,
		registerAnlzConsumer,
		resolveDisplayedAnlz,
		unregisterAnlzConsumer
	} from './anlz-cache.svelte';
	import { ensureBeatgridFallback, getBeatgridFallbackEntry } from './beatgrid-fallback-cache.svelte';
	import { shouldUseBeatgridFallback, toSyntheticAnlzData } from '$lib/rb/beatgrid-fallback';
	import { localDecodeFailureReason } from '$lib/rb/local-waveform-status';
	import { barsToNextCueLabel, followerSyncPlayheadTone } from './wave-math';
	import {
		drawPlayhead,
		drawWaveRow,
		readPalette,
		WAVE_WINDOW_S,
		type PlayheadTone,
		type WavePalette
	} from './render';
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
		if (sid !== null && deckAnlzNeedsFetch(deck.anlz, deck.anlz_error)) ensureAnlz(sid);
	});
	// Keeps a retryable anlz's ambient retry (anlz-cache.svelte.ts) alive for
	// as long as THIS deck holds the track, and releases it the moment the
	// deck unloads or swaps tracks - a bare `ensureAnlz` call carries no such
	// lifetime on its own (issue #735 follow-up, discussion_r3908644098).
	// Deliberately keyed on `deck.stable_id` alone, not `deck.anlz`/
	// `anlz_error`: those change on every retry tick and would otherwise
	// churn the registration for no reason.
	$effect(() => {
		const sid = deck.stable_id;
		if (sid === null) return;
		const token = registerAnlzConsumer(sid);
		return () => unregisterAnlzConsumer(sid, token);
	});
	const anlzData = $derived.by(() => resolveDisplayedAnlz(deck.anlz, deck.stable_id));
	const anlzErrorCode = $derived.by(() => {
		if (deck.anlz_error !== null) return deck.anlz_error;
		if (deck.stable_id === null) return null;
		const entry = getAnlzEntry(deck.stable_id);
		return entry !== undefined && entry.status === 'error' ? entry.code : null;
	});
	// A permanent local-decode failure (issue #735 follow-up): the deck lane
	// otherwise renders blank with no label, since local_waveform never feeds
	// `waveform` and the HTTP response is a normal 200 (discussion_r3908337231).
	const localDecodeFailure = $derived.by(() => localDecodeFailureReason(anlzData));

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

	const masterDeck = $derived(DECK_IDS.find((d) => getDeckState(d).is_master) ?? null);
	const masterState = $derived(masterDeck === null ? null : getDeckState(masterDeck));
	const masterBeats = $derived.by(() => {
		if (masterState === null || masterState.stable_id === null) return null;
		if (masterState.anlz !== null) return masterState.anlz.beatgrid.beats;
		const entry = getAnlzEntry(masterState.stable_id);
		return entry !== undefined && entry.status === 'ready' ? entry.data.beatgrid.beats : null;
	});

	const syncPlayheadTone = $derived.by((): PlayheadTone => {
		if (deck.is_master && deck.stable_id !== null) return 'master';
		const followerBeats = paintAnlz?.beatgrid.beats;
		if (followerBeats === undefined || masterBeats === null || masterState === null) {
			return 'now';
		}
		const tone = followerSyncPlayheadTone({
			beatSyncEnabled: deck.beat_sync_enabled,
			isMaster: deck.is_master,
			syncError: deck.sync_error,
			syncMode: deck.sync_mode,
			followerBeats,
			masterBeats,
			followerPosMs: deck.position_ms,
			masterPosMs: masterState.position_ms
		});
		return tone ?? 'now';
	});

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
	/** SPIKE-PERF: gesture-local paint target. Not published as deck.position_ms
	 * (presentation clock stays transport truth). Undo = always paint deck.position_ms. */
	let scrubPreviewMs: number | null = $state(null);
	let scrubPointerId: number | null = null;
	let scrubOriginClientX = 0;
	let scrubOriginPositionMs = 0;
	let scrubDurationMs = 0;
	let scrubLeftPx = 0;
	let scrubWidthPx = 0;
	let scrubMoved = false;
	let scrubDispatchError: unknown = null;
	const DRAG_THRESHOLD_PX = 3;
	const seekDispatcher = createLatestSeekDispatcher(async (positionMs) => {
		await runPerformanceCommandFromUi({ type: 'seek', deck: deckId, position_ms: positionMs });
	});

	function _queueSeek(positionMs: number): void {
		scrubPreviewMs = positionMs;
		// Drag path must not await: the latest-only dispatcher coalesces while
		// the previous schedule drains. Awaiting here serialized every move
		// behind transport presentation and felt laggy vs Rekordbox.
		void seekDispatcher.request(positionMs).catch((error: unknown) => {
			scrubDispatchError = error;
		});
	}

	function _paintPositionMs(): number {
		return scrubPreviewMs !== null ? scrubPreviewMs : deck.position_ms;
	}

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
		// CH3/4: lighter fill than --rb-bg (#0d0f12) so secondary rows read
		// clearly under the opaque canvas (CSS alone cannot show through).
		const rowBg = deckId === 3 || deckId === 4 ? '#1a1f28' : palette.bg;
		const paintPalette = rowBg === palette.bg ? palette : { ...palette, bg: rowBg };
		if (deck.stable_id === null || deck.duration_ms === null) {
			// Empty deck: flat dark row + always-on now line - never an invented waveform.
			ctx.fillStyle = paintPalette.bg;
			ctx.fillRect(0, 0, cssW, cssH);
			drawPlayhead(ctx, cssW, cssH, 'now');
			return;
		}
		drawWaveRow(ctx, {
			widthCss: cssW,
			heightCss: cssH,
			positionMs: _paintPositionMs(),
			durationMs: deck.duration_ms,
			anlz: paintAnlz,
			palette: paintPalette,
			pitch: deck.pitch,
			loop: deck.loop,
			playheadTone: syncPlayheadTone,
			playheadTimeMs: performance.now()
		});
	}

	// rAF while playing/scrubbing, drift pulse, or master is moving under a synced follower.
	$effect(() => {
		const masterMoving =
			deck.beat_sync_enabled && !deck.is_master && (masterState?.playing ?? false);
		const pulse = syncPlayheadTone === 'drift';
		if (!(deck.playing || seeking || pulse || masterMoving)) return;
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
		const masterMoving =
			deck.beat_sync_enabled && !deck.is_master && (masterState?.playing ?? false);
		if (deck.playing || seeking || syncPlayheadTone === 'drift' || masterMoving) return;
		void deck.stable_id;
		void deck.position_ms;
		void deck.pitch;
		void deck.loop;
		void deck.beat_sync_enabled;
		void deck.sync_mode;
		void deck.sync_error;
		void deck.is_master;
		void syncPlayheadTone;
		void paintAnlz;
		void anlzErrorCode;
		void masterBeats;
		void masterState?.position_ms;
		void cssW;
		void cssH;
		draw();
	});

	// ---- click-drag seek: the engine position at pointerdown is frozen as
	// the gesture origin. Dragging grabs the waveform under the fixed playhead;
	// click-without-drag still seeks to the time visibly beneath the pointer.
	function _dragTarget(clientX: number): number {
		// Match drawWaveRow: wall-clock window scaled by pitch into track time.
		return waveDragTargetMs({
			originPositionMs: scrubOriginPositionMs,
			originClientX: scrubOriginClientX,
			clientX,
			widthPx: scrubWidthPx,
			durationMs: scrubDurationMs,
			windowSeconds: WAVE_WINDOW_S * deck.pitch
		});
	}

	function _clickTarget(clientX: number): number {
		return waveClickTargetMs({
			centerPositionMs: scrubOriginPositionMs,
			pointerX: clientX - scrubLeftPx,
			widthPx: scrubWidthPx,
			durationMs: scrubDurationMs,
			windowSeconds: WAVE_WINDOW_S * deck.pitch
		});
	}

	function _clearGesture(): void {
		scrubPointerId = null;
		scrubMoved = false;
		seeking = false;
		scrubPreviewMs = null;
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
		scrubDispatchError = null;
		seeking = true;
		// SPIKE-PERF: jump the painted window under the pointer immediately.
		scrubPreviewMs = waveClickTargetMs({
			centerPositionMs: scrubOriginPositionMs,
			pointerX: event.clientX - scrubLeftPx,
			widthPx: scrubWidthPx,
			durationMs: scrubDurationMs,
			windowSeconds: WAVE_WINDOW_S * deck.pitch
		});
	}

	async function onPointerMove(event: PointerEvent): Promise<void> {
		if (!seeking || event.pointerId !== scrubPointerId) return;
		const deltaPx = event.clientX - scrubOriginClientX;
		if (!scrubMoved && Math.abs(deltaPx) < DRAG_THRESHOLD_PX) return;
		scrubMoved = true;
		_queueSeek(_dragTarget(event.clientX));
	}

	async function onPointerUp(event: PointerEvent): Promise<void> {
		if (!seeking || event.pointerId !== scrubPointerId) return;
		const canvas = event.currentTarget as HTMLCanvasElement;
		try {
			const targetMs = scrubMoved ? _dragTarget(event.clientX) : _clickTarget(event.clientX);
			scrubPreviewMs = targetMs;
			await seekDispatcher.request(targetMs);
			if (scrubDispatchError !== null) throw scrubDispatchError;
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

<div
	class="rb-waverow"
	class:secondary={deckId === 3 || deckId === 4}
	class:deck-focus={deckHoverUi.deckId === deckId}
	data-deck={deckId}
	use:wheelAdjust={{
		step: WHEEL_STEP.fader,
		get: () => mixerState.channels[deckId].fader,
		set: (value) => void runPerformanceCommandFromUi({ type: 'fader', deck: deckId, value })
	}}
	onpointerenter={() => setHoveredDeck(deckId)}
	onpointerleave={() => {
		if (deckHoverUi.deckId === deckId) setHoveredDeck(null);
	}}
>
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
		{:else if deck.stable_id !== null && localDecodeFailure !== null && beatgridFallback === null}
			<span class="anlz-state" title={localDecodeFailure}>NOT DECODED</span>
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
		/* Strong channel separator so beat lines can be compared across rows. */
		border-bottom: 2px solid #3d4652;
		transition:
			background 50ms ease-out,
			box-shadow 50ms ease-out;
	}
	/* Match mixer CH3/4: gutter/chrome use the same lighter fill as the canvas. */
	.rb-waverow.secondary {
		background: #1a1f28;
	}
	.rb-waverow.deck-focus {
		background: color-mix(in srgb, rgba(255, 255, 255, 0.08) 50%, var(--rb-bg));
		box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.2);
	}
	.rb-waverow.secondary.deck-focus {
		background: color-mix(in srgb, rgba(255, 255, 255, 0.12) 100%, #1a1f28);
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
