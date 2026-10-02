<script lang="ts">
	// Build unit: wavestack - ONE deck row (COMPONENT-MAP 1.2, SCREENSHOT-SPEC 2).
	// Canvas window scrolls under a fixed center playhead; left gutter shows
	// deck identity, artwork, and a readable track title. Empty deck is named,
	// so its reserved artwork slot cannot read as a missing image.
	// rAF repaints ONLY while this deck is playing or being scrubbed.
	import { fetchTrackLyrics } from '$lib/rb/api-rb';
	import {
		performanceCommandStatus,
		queryPerformanceState,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';
	import { hasTrustedBeatGrid } from '$lib/player/grid-features';
	import {
		beatgridFallbackGate,
		paintAnlzForRow,
		readyBeatgridFallback
	} from './wave-row-beatgrid-fallback';
	import {
		ghostSeekBlinkVisible,
		masterAnlzForDeck,
		masterDownbeatOverlayForDeck
	} from './wave-row-deckux-overlays';
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
	import { shouldUseBeatgridFallback } from '$lib/rb/beatgrid-fallback';
	import { localDecodeFailureReason } from '$lib/rb/local-waveform-status';
	import { analysisSourceState } from '$lib/rb/analysis-source.svelte';
	import { noteWaveformPaintFrame, resetWaveformPaintCadence } from '$lib/rb/audio-health.svelte';
	import {
		foldPresentationSample,
		type PresentationStallState
	} from '$lib/player/transport/presentation-stall';
	import { isPresentationClockStalled } from '$lib/rb/presentation-clock-report';
	import {
		initPaintScheduleState,
		initPositionInterpolatorState,
		paintPositionMs,
		paintScrollPx,
		shouldSkipRepaint
	} from './paint-position';
	import { barsToNextCueLabel, followerSyncPlayheadTone } from './wave-math';
	import {
		drawPlayhead,
		drawWaveRow,
		readPalette,
		resolvePaintPalette,
		WAVE_WINDOW_S,
		type PlayheadTone,
		type WavePalette
	} from './render';
	import {
		createLatestSeekDispatcher,
		snapWaveTargetMs,
		waveClickTargetMs,
		waveDragTargetMs,
		waveSnapModeFromModifiers,
		type WaveSnapMode
	} from './wave-scrub';
	import WaveGutter from './WaveGutter.svelte';
	import LyricLanes from './LyricLanes.svelte';
	import StemWaveStack from './StemWaveStack.svelte';
	import { createLyricsFetchState } from './lyrics-fetch.svelte';
	import { waveRowVocalsTitle } from './vocals-title';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { STEM_WAVE_ROW_MAX, STEM_WAVE_ROW_PX } from './stem-waveform-ui';

	const { deckId }: { deckId: DeckId } = $props();

	const deck = $derived(getDeckState(deckId));
	const commandPending = $derived(performanceCommandStatus.deck_pending[deckId] > 0);
	const finished = $derived(
		deck.stable_id !== null && deck.duration_ms !== null && deck.duration_ms > 0 &&
		deck.position_ms >= deck.duration_ms && !deck.audible && !deck.playing &&
		!deck.transport_pending && !commandPending
	);

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
	const lyricsState = createLyricsFetchState(() => deck.stable_id, fetchTrackLyrics);

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

	const fallbackGate = $derived(
		beatgridFallbackGate({
			anlzErrorCode,
			anlz: anlzData,
			effectiveSource: analysisSourceState.features.beatgrid
		})
	);
	$effect(() => {
		const sid = deck.stable_id;
		if (sid !== null && shouldUseBeatgridFallback(fallbackGate)) ensureBeatgridFallback(sid);
	});
	const beatgridFallback = $derived(
		readyBeatgridFallback(deck.stable_id, fallbackGate, getBeatgridFallbackEntry)
	);
	const paintAnlz = $derived(paintAnlzForRow(anlzData, beatgridFallback));

	// Bars until next cue; null (hidden) without a beatgrid or upcoming cue.
	const barsLabel = $derived(
		paintAnlz !== null ? barsToNextCueLabel(paintAnlz, deck.position_ms) : null
	);

	// Scrub-target snapping (pin a705aebfbeae) reads this deck's own beatgrid,
	// not the master's - each waveform snaps to its own track's downbeats.
	const scrubBeats = $derived(paintAnlz?.beatgrid.beats ?? null);

	const masterDeck = $derived(DECK_IDS.find((d) => getDeckState(d).is_master) ?? null);
	const masterState = $derived(masterDeck === null ? null : getDeckState(masterDeck));
	const masterAnlz = $derived(masterAnlzForDeck(masterState, getAnlzEntry));
	const masterBeats = $derived(masterAnlz?.beatgrid.beats ?? null);

	const syncPlayheadTone = $derived.by((): PlayheadTone => {
		if (!deck.audible) return 'stopped';
		if (deck.is_master && deck.stable_id !== null) {
			// MASTER remains yellow; green is reserved for the explicit Beat
			// Sync-enabled state, even on the MASTER itself.
			return deck.beat_sync_enabled ? 'masterSynced' : 'master';
		}
		const followerBeats = paintAnlz?.beatgrid.beats;
		if (followerBeats === undefined || masterBeats === null || masterState === null) {
			return 'now';
		}
		const tone = followerSyncPlayheadTone({
			beatSyncEnabled: deck.beat_sync_enabled,
			isMaster: deck.is_master,
			syncError: deck.sync_error,
			syncMode: deck.sync_mode,
			followerBeats: [...followerBeats],
			masterBeats: [...masterBeats],
			followerPosMs: deck.position_ms,
			masterPosMs: masterState.position_ms
		});
		return tone ?? 'now';
	});

	// Vocal state tooltip (SPIKE-B1/B2 four mandatory states): see vocals-title.ts.
	const vocalsTitle = $derived(waveRowVocalsTitle(anlzData));

	const showStems = $derived(uiPrefs.show_stems);
	const waveformSeekArmed = $derived(queryPerformanceState().decks[deckId].waveform_seek_armed);
	const ghostBlinkOn = $derived(ghostSeekBlinkVisible(performance.now()));
	const masterDownbeatOverlay = $derived.by(() =>
		masterDownbeatOverlayForDeck({
			beatSyncMax: uiPrefs.beat_sync_max,
			masterState,
			masterAnlz,
			deckPositionMs: deck.position_ms,
			deckPitch: deck.pitch,
			hasTrustedBeatGrid
		})
	);

	// ---- canvas plumbing
	let canvasEl: HTMLCanvasElement | undefined = $state();
	let cssW = $state(0);
	let cssH = $state(0);
	// Reactive so the stopped-deck repaint effect below redraws when a theme
	// or waveform palette switch re-reads it; as a plain let, an idle deck kept
	// the old colors until the next seek (issue #4219, Mac check on PR #4923).
	let palette = $state.raw<WavePalette | null>(null);
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
	const _paintScheduleState = initPaintScheduleState();
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

	// Pin 53ba89ca8ddc (waveform jitter): see `./paint-position.ts` for the
	// measurement + rationale (tested there, a `.svelte` file cannot be).
	// `stallState` below folds raw `deck.position_ms`, never this value.
	const _paintPositionState = initPositionInterpolatorState();

	function _paintPositionMs(): number {
		return paintPositionMs(_paintPositionState, scrubPreviewMs, deck, clockUntrusted, performance.now());
	}

	const stemScrollPx = $derived(
		paintScrollPx(_paintPositionMs(), deck.duration_ms, cssW, WAVE_WINDOW_S, deck.pitch)
	);
	const stemRowExtraPx = $derived(
		showStems && deck.stable_id !== null ? STEM_WAVE_ROW_MAX * STEM_WAVE_ROW_PX : 0
	);

	$effect(() => {
		const el = canvasEl;
		if (!el) return;
		// Re-resolve the CSS-var palette when the theme or the waveform band
		// palette changes (issue #4219): both swap the --rb-wave-* vars.
		void uiPrefs.theme;
		void uiPrefs.wave_palette;
		palette = readPalette(el); // throws if not under .perf-root
		const observer = new ResizeObserver((entries) => {
			const rect = entries[0].contentRect;
			cssW = Math.round(rect.width);
			cssH = Math.round(rect.height);
		});
		observer.observe(el);
		return () => observer.disconnect();
	});

	function draw(force = false): void {
		const el = canvasEl;
		if (!el || palette === null || cssW === 0 || cssH === 0) return;
		const paintPositionMs = _paintPositionMs();
		const scrollPx = paintScrollPx(paintPositionMs, deck.duration_ms, cssW, WAVE_WINDOW_S, deck.pitch);
		const visualInputs = [
			deck.stable_id,
			deck.duration_ms,
			paintAnlz,
			deck.pitch,
			deck.loop,
			syncPlayheadTone,
			cssW,
			cssH,
			palette
		] as const;
		if (shouldSkipRepaint(_paintScheduleState, force, visualInputs, scrollPx)) return;
		const dpr = window.devicePixelRatio;
		if (el.width !== cssW * dpr || el.height !== cssH * dpr) {
			el.width = cssW * dpr;
			el.height = cssH * dpr;
		}
		const ctx = el.getContext('2d');
		if (ctx === null) throw new Error(`wavestack deck ${deckId}: 2d context unavailable`);
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		const paintPalette = resolvePaintPalette(deckId, palette);
		if (deck.stable_id === null || deck.duration_ms === null) {
			// Empty deck: flat dark row + state-derived center line - never an invented waveform.
			ctx.fillStyle = paintPalette.bg;
			ctx.fillRect(0, 0, cssW, cssH);
			drawPlayhead(ctx, cssW, cssH, syncPlayheadTone);
			return;
		}
		drawWaveRow(ctx, {
			widthCss: cssW,
			heightCss: cssH,
			positionMs: paintPositionMs,
			durationMs: deck.duration_ms,
			anlz: paintAnlz,
			palette: paintPalette,
			pitch: deck.pitch,
			loop: deck.loop,
			playheadTone: syncPlayheadTone,
			playheadTimeMs: performance.now(),
			waveformDesign: uiPrefs.waveform_design,
			masterDownbeatOverlay,
			ghostSeekMs: waveformSeekArmed?.target_position_ms ?? null,
			ghostSeekVisible: waveformSeekArmed !== null && ghostBlinkOn
		});
	}

	// Is the painted position actually moving? deck.playing is desired INTENT
	// (audio-engine sets st.playing = rt.desiredActive), not presented truth, so
	// this loop will happily repaint a pixel-identical frame at 60Hz forever and
	// call it healthy - which is exactly what it did for twenty minutes on
	// Wed 2 Sep 2026. Nothing here can fix a frozen clock; what it can do is stop
	// lying about it. See .planning/hardening-ledger/items/
	// waveform-freezes-on-stale-output-timestamp.md.
	let stallState = $state.raw<PresentationStallState | undefined>(undefined);
	let playheadFrozen = $state(false);
	// Either the device clock stalled (presentation.ts is coasting on the sample
	// clock) or the painted number itself stopped moving for any other reason.
	const clockUntrusted = $derived(playheadFrozen || isPresentationClockStalled(deckId));
	// A synced-but-not-master follower riding the master's motion, so it must
	// keep painting while `deck.playing` is false. Read by both effects below.
	const masterMoving = $derived(deck.beat_sync_enabled && !deck.is_master && (masterState?.playing ?? false));

	// rAF while playing/scrubbing, drift pulse, or master is moving under a synced follower.
	$effect(() => {
		const pulse = syncPlayheadTone === 'drift';
		const hovered = deckHoverUi.deckId === deckId;
		if (!(deck.playing || seeking || waveformSeekArmed !== null || (hovered && (pulse || masterMoving)))) return;
		let raf = requestAnimationFrame(function waveRowFrame(timestamp) {
			draw();
			if (cssW > 0 && cssH > 0 && !document.hidden) noteWaveformPaintFrame(deckId, timestamp);
			stallState = foldPresentationSample(stallState, {
				playing: deck.playing,
				position_ms: deck.position_ms,
				tMs: performance.now()
			});
			const stall = stallState;
			if (stall !== undefined && stall.verdict === 'presentation-stalled') playheadFrozen = true;
			else if (stall !== undefined && stall.frozenSinceMs === null) playheadFrozen = false;
			raf = requestAnimationFrame(waveRowFrame);
		});
		const onVisibilityChange = () => { if (document.hidden) resetWaveformPaintCadence(deckId); };
		document.addEventListener('visibilitychange', onVisibilityChange);
		return () => { cancelAnimationFrame(raf); document.removeEventListener('visibilitychange', onVisibilityChange); resetWaveformPaintCadence(deckId); };
	});

	// Static repaint on load/seek/resize/anlz-arrival while stopped. The
	// early return keeps position_ms untracked during playback so this
	// effect stays quiet while the rAF loop owns the canvas.
	$effect(() => {
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
		void palette;
		draw(true);
	});

	// ---- click-drag seek: the engine position at pointerdown is frozen as
	// the gesture origin. Dragging grabs the waveform under the fixed playhead;
	// click-without-drag still seeks to the time visibly beneath the pointer.
	function _dragTarget(clientX: number, modifiers: { shiftKey: boolean; metaKey: boolean }): number {
		// Match drawWaveRow: wall-clock window scaled by pitch into track time.
		const raw = waveDragTargetMs({
			originPositionMs: scrubOriginPositionMs,
			originClientX: scrubOriginClientX,
			clientX,
			widthPx: scrubWidthPx,
			durationMs: scrubDurationMs,
			windowSeconds: WAVE_WINDOW_S * deck.pitch
		});
		return snapWaveTargetMs(raw, scrubBeats, waveSnapModeFromModifiers(modifiers));
	}

	function _clickTarget(clientX: number, modifiers: { shiftKey: boolean; metaKey: boolean }): number {
		const raw = waveClickTargetMs({
			centerPositionMs: scrubOriginPositionMs,
			pointerX: clientX - scrubLeftPx,
			widthPx: scrubWidthPx,
			durationMs: scrubDurationMs,
			windowSeconds: WAVE_WINDOW_S * deck.pitch
		});
		return snapWaveTargetMs(raw, scrubBeats, waveSnapModeFromModifiers(modifiers));
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
		const snapOnDown = waveSnapModeFromModifiers(event);
		const deferBeatSyncSeek =
			snapOnDown === 'downbeat' && deck.playing && uiPrefs.beat_sync_max;
		// SPIKE-PERF: jump the painted window under the pointer immediately,
		// except BeatSyncMax deferred seeks (ghost cursor until arm fires).
		if (!deferBeatSyncSeek) {
			scrubPreviewMs = _clickTarget(event.clientX, event);
		}
	}

	async function onPointerMove(event: PointerEvent): Promise<void> {
		if (!seeking || event.pointerId !== scrubPointerId) return;
		const deltaPx = event.clientX - scrubOriginClientX;
		if (!scrubMoved && Math.abs(deltaPx) < DRAG_THRESHOLD_PX) return;
		scrubMoved = true;
		_queueSeek(_dragTarget(event.clientX, event));
	}

	async function onPointerUp(event: PointerEvent): Promise<void> {
		if (!seeking || event.pointerId !== scrubPointerId) return;
		const canvas = event.currentTarget as HTMLCanvasElement;
		try {
			const snap: WaveSnapMode = waveSnapModeFromModifiers(event);
			const targetMs = scrubMoved
				? _dragTarget(event.clientX, event)
				: _clickTarget(event.clientX, event);
			if (!scrubMoved && snap === 'downbeat' && deck.playing && uiPrefs.beat_sync_max) {
				scrubPreviewMs = null;
				await runPerformanceCommandFromUi({
					type: 'waveform_seek',
					deck: deckId,
					position_ms: targetMs,
					snap
				});
			} else {
				scrubPreviewMs = targetMs;
				await seekDispatcher.request(targetMs);
				if (scrubDispatchError !== null) throw scrubDispatchError;
			}
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
	data-deck={deckId} data-wave-surface="row"
	style={`--rb-waverow-stem-extra: ${stemRowExtraPx}px`}
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
	<WaveGutter {deck} {deckId} {barsLabel} />
	<div class="wave-col">
		<div class="canvas-wrap" title={vocalsTitle ?? undefined}>
			{#if finished}
				<button
					class="finished-eject"
					title={`Eject ${deck.title ?? 'track'} from deck ${deckId}`}
					onclick={() => runPerformanceCommandFromUi({ type: 'unload', deck: deckId })}
				>
					⏏ {deck.title ?? 'Track'} - deck {deckId}
				</button>
			{/if}
			{#if clockUntrusted}
				<span
					class="clock-stalled"
					title="The audio device stopped reporting where playback is. The waveform is
estimated from the render clock and may run ahead of what you hear."
				>
					CLOCK
				</span>
			{/if}
			<canvas
				class="wave-seek-canvas"
				bind:this={canvasEl}
				role="slider"
				aria-label="deck {deckId} waveform seek"
				aria-valuemin={0}
				aria-valuemax={deck.duration_ms ?? 0}
				aria-valuenow={Math.round(deck.position_ms)}
				aria-disabled={deck.stable_id === null || (commandPending && !seeking)}
				tabindex="-1"
				data-hotkey-pointer-only
				onpointerdown={onPointerDown}
				onpointermove={onPointerMove}
				onpointerup={onPointerUp}
				onpointercancel={onPointerCancel}
				onlostpointercapture={onLostPointerCapture}
			></canvas>
			<LyricLanes stableId={deck.stable_id} lyrics={lyricsState.lyrics} loadError={lyricsState.loadError} positionMs={_paintPositionMs()} pitch={deck.pitch} />
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
		<StemWaveStack {deck} {showStems} scrollPx={stemScrollPx} canvasWidth={cssW} />
	</div>
</div>

<style>
	@import './WaveRow.chrome.css';

	/* Match mixer CH3/4 intent: 3/4 recede as the lighter fill. Solid, not
	   mixer's translucent panel-raised mix, because the canvas is opaque.
	   Kept inline (not in WaveRow.chrome.css) so this file's own source text
	   still carries the exact selectors tests/unit/wave-track-summary-
	   emphasis.test.mjs reads for the A11Y-03 dim-token contrast check. */
	.rb-waverow.secondary {
		background: var(--rb-waverow-secondary);
	}
	.rb-waverow.secondary.deck-focus {
		background: color-mix(in srgb, rgba(255, 255, 255, 0.12) 100%, var(--rb-waverow-secondary));
	}
</style>
