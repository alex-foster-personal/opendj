<script lang="ts">
	// Build unit: wavestack - ONE deck row (COMPONENT-MAP 1.2, SCREENSHOT-SPEC 2).
	// Canvas window scrolls under a fixed center playhead; left gutter shows
	// deck identity, artwork, the track title and its live key and BPM
	// (WaveGutter, DECKUX-40). An empty deck shows only its number.
	// rAF repaints ONLY while this deck is playing or being scrubbed.
	import { untrack } from 'svelte';
	import { fetchTrackLyrics } from '$lib/rb/api-rb';
	import {
		performanceCommandStatus,
		queryWaveformSeekArmed,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';
	import { effectiveQuantize, hasTrustedBeatGrid } from '$lib/player/grid-features';
	import {
		beatgridFallbackGate,
		paintAnlzForRow,
		readyBeatgridFallback
	} from './wave-row-beatgrid-fallback';
	import {
		ghostSeekFrame,
		masterAnlzForDeck,
		masterDownbeatOverlayForDeck,
		masterGridGhostForDeck
	} from './wave-row-deckux-overlays';
	import type { DeckId } from '$lib/rb/deck-slots';
	import { getDeckState, DECK_IDS, mixerState } from './engine-accessor';
	import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';
	import { deckHoverLinkId, deckHoverUi, setHoveredDeck } from '$lib/rb/deck-hover.svelte';
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
	import { playheadMs } from '$lib/rb/playhead-display.svelte';
	import { tracePlayhead } from '$lib/rb/playhead-trace';
	import {
		initPaintScheduleState,
		paintScrollPx,
		shouldSkipRepaint
	} from './paint-position';
	import { followerSyncPlayheadTone } from './wave-math';
	import {
		drawPlayhead,
		drawWaveRow,
		readPalette,
		resolvePaintPalette,
		type PlayheadTone,
		type WavePalette
	} from './render';
	import {
		createLatestSeekDispatcher,
		snapWaveTargetMs,
		waveClickSeekMs,
		waveDragTargetMs,
		waveSeekCommand,
		waveSnapModeFromModifiers
	} from './wave-scrub';
	import WaveGutter from './WaveGutter.svelte';
	import LyricLanes from './LyricLanes.svelte';
	import StemWaveStack from './StemWaveStack.svelte';
	import { createLyricsFetchState } from './lyrics-fetch.svelte';
	import { waveRowVocalsTitle } from './vocals-title';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { paintBandGains } from '$lib/rb/wave-eq-gains.svelte';
	import { effectiveWaveformDesign, waveSplitActive } from '$lib/rb/ui-skin';
	import { paintSplitRow, splitPartnerDeck } from './split-row';
	import { sectionsApiFor } from './sections-lazy.svelte';
	import { STEM_WAVE_ROW_MAX, STEM_WAVE_ROW_PX } from './stem-waveform-ui';
	import { deckWaveZoom, type WaveZoom } from '$lib/rb/wave-zoom';
	import {
		setWaveZoomGlideRunning,
		setWaveZoomRowBeats,
		setWaveZoomScrubPreviewMs,
		setWaveZoomScrubWindowS,
		waveZoomGlideRunning,
		waveZoomRowBeats,
		waveZoomScrubPreviewMs,
		waveZoomScrubWindowS,
	} from '$lib/rb/wave-zoom-glide-state.svelte';
	import {
		WAVE_ZOOM_ANIM_MS,
		setWaveZoomGlide,
		beatgridVariesTempo,
		waveZoomRefAt,
		waveZoomSharedWindowS,
		waveZoomAnimatedWindowS,
		waveZoomAnimating,
		waveZoomAnimFromS,
		waveZoomLabel,
		wavePlayheadFrac,
		waveZoomRef,
		waveZoomWheelDeltaPx,
		waveZoomWheelSteps,
		waveZoomWindowS,
		type WaveZoomAnim
	} from '$lib/rb/wave-zoom-view';
	import { planAnchorMoved, planDeckOverlay } from '$lib/rb/plan-overlay-store.svelte';
	import { planChipText, planChipTitle } from '$lib/rb/plan-overlay-chip';
	import { tempoCurveData, tempoStabilityKey } from '$lib/rb/tempo-stability';
	import { requestTempoStability, tempoStabilityFor } from '$lib/rb/tempo-stability.svelte';
	import { effectiveWaveScheme } from '$lib/rb/ui-skin';
	import { TEMPO_LANE_COLORS } from './tempo-marks';
	import { backingSize, RESIZE_SETTLE_MS } from './canvas-backing';

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
	// Publish the beats this row paints, so every row sizes the reference's bars
	// from the same analysis the reference row draws (WAVEZOOM-6802).
	$effect(() => {
		setWaveZoomRowBeats(deckId, paintAnlz?.beatgrid.beats ?? null);
		return () => setWaveZoomRowBeats(deckId, null);
	});


	// Scrub-target snapping (WAVE-SEEK-01) reads this deck's own beatgrid, the
	// one the engine quantizes with (deck.anlz), not the master's, and only when
	// the deck's quantize is on: then it lands on the NEAREST beat.
	const scrubBeats = $derived(deck.anlz?.beatgrid.beats ?? null);
	const scrubQuantize = $derived(effectiveQuantize(deck));

	const masterDeck = $derived(DECK_IDS.find((d) => getDeckState(d).is_master) ?? null);
	const masterState = $derived(masterDeck === null ? null : getDeckState(masterDeck));
	const masterAnlz = $derived(masterAnlzForDeck(masterState, getAnlzEntry));
	const masterBeats = $derived(masterAnlz?.beatgrid.beats ?? null);

	// Split main waveform (wave_split_master, opt-in): partner deck on the
	// top half; the master row has no partner, so it paints plain mirrored.
	const splitOn = $derived(waveSplitActive(uiPrefs.wave_split_master));
	const splitPartner = $derived(
		splitOn
			? splitPartnerDeck(
					deckId,
					DECK_IDS.map((id) => {
						const st = getDeckState(id);
						return { id, loaded: st.stable_id !== null && st.duration_ms !== null, isMaster: st.is_master };
					})
				)
			: null
	);
	const partnerState = $derived(splitPartner === null ? null : getDeckState(splitPartner.id));
	const partnerAnlz = $derived(masterAnlzForDeck(partnerState, getAnlzEntry));

	// ---- WAVEZOOM-6802..06: zoom step, step animation and elastic playhead.
	// The reference deck (master, else the first loaded deck) sizes a bar and
	// "whole track"; its position is read live at paint time, never from here,
	// so this derived does not re-run on every position sample.
	const zoomStep = $derived(deckWaveZoom(uiPrefs, deckId));
	const zoomRefDeck = $derived(
		waveZoomRef(
			DECK_IDS.map((id) => {
				const st = getDeckState(id);
				return {
					id,
					// A deck is a reference only once its length is published: the load sets
					// stable_id before duration_ms, and an incomplete load must not elect.
					loaded: st.stable_id !== null && st.duration_ms !== null,
					isMaster: st.is_master,
					// Bars follow the beatgrid the waveform draws; the tag is the fallback.
					bpm: st.anlz?.beatgrid.bpm ?? st.bpm,
					pitch: st.pitch,
					durationMs: st.duration_ms,
					positionMs: 0
				};
			})
		)
	);
	let zoomAnim = $state.raw<WaveZoomAnim | null>(null);
	let zoomAnimMs = WAVE_ZOOM_ANIM_MS;
	/** True while a step animates, so the rAF loop runs on a stopped deck too. */
	let zoomAnimActive = $state(false);
	let zoomChipVisible = $state(false);
	/** Bumped each animated frame so the DOM lanes (lyrics, stems) follow the
	 * glide; the canvas reads the live view itself every frame. */
	let zoomFrame = $state(0);
	let _prevZoom: WaveZoom | null = null;
	$effect(() => {
		const next = zoomStep;
		const prev = _prevZoom;
		_prevZoom = next;
		if (prev === null || prev === next) return;
		const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;
		const now = performance.now();
		// A step that lands mid-glide starts from the window on screen, not from
		// the previous step's target, so the waveform does not jump.
		const shown = untrack(() => {
			if (zoomAnim === null || !waveZoomAnimating(zoomAnim, now, zoomAnimMs)) return undefined;
			const ref =
				zoomRefDeck === null
					? null
					: waveZoomRefAt(zoomRefDeck, getDeckState(zoomRefDeck.id).position_ms, waveZoomRowBeats(zoomRefDeck.id));
			return waveZoomAnimatedWindowS(zoomAnim, ref, now, zoomAnimMs);
		});
		// Plain locals feed the writes and the setters: reading the $state back here
		// would subscribe this effect to its own writes and cancel the glide it starts.
		const durMs = reduced ? 0 : WAVE_ZOOM_ANIM_MS;
		const anim = { from: prev, to: next, startMs: now, fromWindowS: shown };
		zoomAnimMs = durMs;
		zoomAnim = anim;
		setWaveZoomGlide(deckId, anim, durMs);
		zoomAnimActive = durMs > 0;
		setWaveZoomGlideRunning(deckId, durMs > 0);
		zoomChipVisible = true;
		const animTimer = setTimeout(() => {
			zoomAnimActive = false;
			setWaveZoomGlideRunning(deckId, false);
		}, durMs + 32);
		const chipTimer = setTimeout(() => (zoomChipVisible = false), 1200);
		return () => {
			clearTimeout(animTimer);
			clearTimeout(chipTimer);
			setWaveZoomGlideRunning(deckId, false);
		};
	});
	// One chip for the stack while the zoom is linked (on the top row), else
	// one on each row whose own step changed.
	const zoomChipHere = $derived(zoomChipVisible && (!uiPrefs.wave_zoom_linked || deckId === 1));
	// Elastic: once zoomed out past half the reference track the shared
	// playhead column follows the reference deck, so every row keeps painting
	// while that deck plays, the reference row included, so its lanes and
	// overlays advance per frame with its canvas (not per transport sample).
	const elasticFollowing = $derived.by(() => {
		const ref = zoomRefDeck;
		if (uiPrefs.wave_zoom_playhead !== 'elastic' || ref === null) return false;
		if (!getDeckState(ref.id).playing || ref.durationMs === null) return false;
		const live = waveZoomRefAt(ref, getDeckState(ref.id).position_ms, waveZoomRowBeats(ref.id));
		return waveZoomWindowS(deckWaveZoom(uiPrefs, ref.id), live) > ref.durationMs / 1000 / ref.pitch / 2;
	});
	// The reference's own glide or scrub keeps every follower row repainting while
	// it runs, even when the follower is stopped and not elastic-following.
	const refMoving = $derived(
		zoomRefDeck !== null &&
			zoomRefDeck.id !== deckId &&
			(waveZoomGlideRunning(zoomRefDeck.id) || waveZoomScrubPreviewMs(zoomRefDeck.id) !== null)
	);

	// A playing reference whose beatgrid varies in tempo changes its bar length
	// as it crosses each tempo boundary. Every row, the reference's own included,
	// must tick with it: lyric lanes, stem rows and stopped followers keep the
	// previous scale otherwise, until the next transport publication. A constant
	// grid never changes, so it costs no per-frame work.
	const refTempoLive = $derived.by(() => {
		const ref = zoomRefDeck;
		if (ref === null || !getDeckState(ref.id).playing) return false;
		return beatgridVariesTempo(waveZoomRowBeats(ref.id));
	});

	/** The row's zoom view as painted: the live view, with the window held during a drag. */
	function _zoomView(paintPositionMs: number): { windowS: number; frac: number; cacheWindowS?: number } {
		const view = _liveZoomView(paintPositionMs);
		// A held scale is the reference's for the whole gesture, so LINKED follower
		// rows share it. Unlinked rows keep their own zoom. The playhead fraction
		// stays live: it is the shared column.
		const linkedRef = uiPrefs.wave_zoom_linked && zoomRefDeck !== null ? zoomRefDeck : null;
		const held = scrubFrozenWindowS ?? (linkedRef === null ? null : waveZoomScrubWindowS(linkedRef.id));
		if (held === null) return view;
		return { windowS: held, frac: view.frac };
	}

	/** The row's live zoom view: wall-clock window and playhead place 0..1. */
	function _liveZoomView(paintPositionMs: number): { windowS: number; frac: number; cacheWindowS?: number } {
		const ref = zoomRefDeck;
		// The reference's bar length follows the beatgrid tempo at its current
		// position, so a variable-tempo grid keeps the ladder honest (WAVEZOOM-6802).
		const live =
			ref === null
				? null
				: waveZoomRefAt(
						ref,
						ref.id === deckId ? paintPositionMs : _partnerPaintMs(ref.id, getDeckState(ref.id)),
						waveZoomRowBeats(ref.id)
					);
		const anim = zoomAnim;
		const now = performance.now();
		const gliding = anim !== null && anim.to === zoomStep && waveZoomAnimating(anim, now, zoomAnimMs);
		const windowS = gliding ? waveZoomAnimatedWindowS(anim, live, now, zoomAnimMs) : waveZoomWindowS(zoomStep, live);
		// The playhead column is shared: every row places it from the reference
		// deck's window, animated while the reference glides, so unlinked rows at
		// different steps stay on one column through every glide.
		const sharedWindowS = live === null || ref === null ? windowS : waveZoomSharedWindowS(ref.id, deckWaveZoom(uiPrefs, ref.id), live, now);
		const frac = wavePlayheadFrac(uiPrefs.wave_zoom_playhead, sharedWindowS, live);
		if (!gliding) return { windowS, frac };
		// Build the band cache once, at the wider step, and scale it through the glide.
		const cacheWindowS = Math.max(waveZoomAnimFromS(anim, live), waveZoomWindowS(anim.to, live));
		return { windowS, frac, cacheWindowS };
	}

	// Ctrl/Cmd-wheel and trackpad pinch (which arrives as a ctrl-wheel) zoom
	// one ladder step per WAVE_ZOOM_WHEEL_STEP_PX of delta. Registered
	// non-passive on the canvas wrap, below the row's fader wheelAdjust, and
	// stopped there so the channel fader never moves on a zoom gesture.
	const _zoomWheelAcc = { px: 0 };
	let canvasWrapEl: HTMLDivElement | undefined = $state();
	$effect(() => {
		const el = canvasWrapEl;
		if (!el) return;
		const onWheel = (event: WheelEvent): void => {
			if (!(event.ctrlKey || event.metaKey)) return;
			event.preventDefault();
			event.stopPropagation();
			const deltaPx = waveZoomWheelDeltaPx(event.deltaY, event.deltaMode, el.clientHeight);
			// One step per threshold crossed: a single large delta can complete several.
			const steps = waveZoomWheelSteps(_zoomWheelAcc, deltaPx);
			for (let i = 0; i < Math.abs(steps); i++) {
				void runPerformanceCommandFromUi({
					type: 'wave_zoom_step',
					direction: steps > 0 ? 'in' : 'out',
					deck: deckId
				});
			}
		};
		el.addEventListener('wheel', onWheel, { passive: false });
		return () => el.removeEventListener('wheel', onWheel);
	});
	// WAVEPLAN-7896: a pending plan's curves on this deck and on the split row's partner. Null with no plan.
	const planCurves = $derived(planDeckOverlay(deckId));
	// Issue #8339: tempo-change marks with direction and severity, from the
	// same daemon read the deck strip's tempo lane paints.
	const tempoKey: string | null = $derived(
		deck.stable_id === null || anlzData === null ? null : tempoStabilityKey(deck.stable_id, anlzData.beatgrid)
	);
	$effect(() => {
		if (tempoKey !== null && deck.stable_id !== null) requestTempoStability(deck.stable_id, tempoKey);
	});
	const tempoSegments = $derived(tempoCurveData(tempoKey === null ? null : tempoStabilityFor(tempoKey)));
	const partnerTempoKey: string | null = $derived(
		partnerState === null || partnerState.stable_id === null || partnerAnlz === null
			? null
			: tempoStabilityKey(partnerState.stable_id, partnerAnlz.beatgrid)
	);
	$effect(() => {
		if (partnerTempoKey !== null && partnerState !== null && partnerState.stable_id !== null) {
			requestTempoStability(partnerState.stable_id, partnerTempoKey);
		}
	});
	const partnerTempoSegments = $derived(
		tempoCurveData(partnerTempoKey === null ? null : tempoStabilityFor(partnerTempoKey))
	);
	const tempoScale = $derived(uiPrefs.tempo_lane_scale);
	const hideTempo = $derived(!uiPrefs.show_tempo_lane);
	const tempoColors = $derived(TEMPO_LANE_COLORS[effectiveWaveScheme(uiPrefs.theme, uiPrefs.ui_skin)]);
	const partnerPlanCurves = $derived(splitPartner === null ? null : planDeckOverlay(splitPartner.id));

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
	const waveformSeekArmed = $derived(queryWaveformSeekArmed(deckId));
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
	// Faint master beat grid on every other row, whether or not Beat Sync Max
	// is on. The master row keeps its own grid and does not ghost itself.
	const masterBeatGhost = $derived.by(() =>
		masterGridGhostForDeck({
			isMaster: deck.is_master,
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
	// The painted window scale, captured at pointerdown and held for the gesture. The drag
	// maps pointer travel to time with one scale, so the content under the pointer must not
	// stretch while the preview crosses a variable-tempo boundary. The playhead fraction
	// stays live: it is the shared column that followers track.
	let scrubFrozenWindowS: number | null = null;
	const _paintScheduleState = initPaintScheduleState();
	const DRAG_THRESHOLD_PX = 3;
	const seekDispatcher = createLatestSeekDispatcher(async (positionMs) => {
		await runPerformanceCommandFromUi(waveSeekCommand(deckId, positionMs));
	});

	/** Mirrors this row's gesture target into the shared registry, so follower rows
	 * place the shared playhead from the same scrub position the reference paints. */
	function _setScrubPreview(ms: number | null): void {
		scrubPreviewMs = ms;
		setWaveZoomScrubPreviewMs(deckId, ms);
	}
	// Teardown mid-gesture must clear the shared held scale too, or linked rows
	// would keep the old gesture's window after this row unmounts.
	$effect(() => () => {
		setWaveZoomScrubPreviewMs(deckId, null);
		setWaveZoomScrubWindowS(deckId, null);
	});

	function _queueSeek(positionMs: number): void {
		_setScrubPreview(positionMs);
		// Drag path must not await: the latest-only dispatcher coalesces while
		// the previous schedule drains. Awaiting here serialized every move
		// behind transport presentation and felt laggy vs Rekordbox.
		void seekDispatcher.request(positionMs).catch((error: unknown) => {
			scrubDispatchError = error;
		});
	}

	// Every playhead element draws from one clock per deck (ANIM-CLOCK-01,
	// `$lib/rb/playhead-clock.ts`): dead reckoning from the engine's output
	// timestamp, small drift slewed out, discontinuities snapped. A scrub wins
	// outright; an untrusted clock paints the raw sample, so a stall stays
	// visible. `stallState` below folds raw `deck.position_ms`, never this value.
	function _paintPositionMs(): number {
		if (scrubPreviewMs !== null) return scrubPreviewMs;
		return clockUntrusted ? deck.position_ms : playheadMs(deckId, deck);
	}

	function _partnerPaintMs(id: DeckId, partner: { position_ms: number; playing: boolean }): number {
		const preview = waveZoomScrubPreviewMs(id);
		if (preview !== null) return preview;
		return isPresentationClockStalled(id) ? partner.position_ms : playheadMs(id, partner);
	}

	const laneView = $derived.by(() => {
		void zoomFrame;
		void zoomAnimActive;
		return _zoomView(_paintPositionMs());
	});
	const stemScrollPx = $derived(
		paintScrollPx(_paintPositionMs(), deck.duration_ms, cssW, laneView.windowS, deck.pitch)
	);
	const stemRowExtraPx = $derived(
		showStems && deck.stable_id !== null ? STEM_WAVE_ROW_MAX * STEM_WAVE_ROW_PX : 0
	);

	// PERF-PAINT-10546: true for RESIZE_SETTLE_MS after the last size change,
	// so the backing store grows in buckets instead of every frame.
	let backingResizing = $state(false);
	let settleTimer: ReturnType<typeof setTimeout> | null = null;

	$effect(() => {
		const el = canvasEl;
		// Svelte 5 sets bind:this to null on unmount (issue #1392), not undefined.
		if (el == null) return;
		// Re-resolve the CSS-var palette when the theme or the waveform band
		// palette or the skin changes (issue #4219, skin cycle): all three swap
		// the --rb-wave-* vars.
		void uiPrefs.theme;
		void uiPrefs.wave_palette;
		void uiPrefs.ui_skin;
		palette = readPalette(el); // throws if not under .perf-root
		const wrap = canvasWrapEl;
		if (wrap == null) return;
		const observer = new ResizeObserver((entries) => {
			const rect = entries[0].contentRect;
			const nextW = Math.round(rect.width);
			const nextH = Math.round(rect.height);
			// The first report sizes the canvas exactly; only later changes are a resize.
			if (cssW !== 0 && cssH !== 0 && (nextW !== cssW || nextH !== cssH)) {
				backingResizing = true;
				if (settleTimer !== null) clearTimeout(settleTimer);
				settleTimer = setTimeout(() => {
					settleTimer = null;
					backingResizing = false;
				}, RESIZE_SETTLE_MS);
			}
			cssW = nextW;
			cssH = nextH;
		});
		// Observe the wrap, not the canvas: the canvas is styled to the bucket
		// size while resizing and would feed that back as cssW/cssH.
		observer.observe(wrap);
		return () => {
			observer.disconnect();
			if (settleTimer !== null) clearTimeout(settleTimer);
			settleTimer = null;
		};
	});

	function draw(force = false): void {
		const el = canvasEl;
		if (!el || palette === null || cssW === 0 || cssH === 0) return;
		const paintPositionMs = _paintPositionMs();
		const view = _zoomView(paintPositionMs);
		const scrollPx = paintScrollPx(paintPositionMs, deck.duration_ms, cssW, view.windowS, deck.pitch);
		const ghost = ghostSeekFrame(waveformSeekArmed, performance.now());
		// WAVE-EQ-5230: identity changes only when a gain moves, so a paused deck
		// repaints on an EQ turn and never otherwise.
		const bandGains = paintBandGains(deckId, uiPrefs.wave_eq_reflect);
		const partnerPaintMs = splitPartner === null || partnerState === null ? null : _partnerPaintMs(splitPartner.id, partnerState);
		const partnerGains = splitPartner === null ? null : paintBandGains(splitPartner.id, uiPrefs.wave_eq_reflect);
		const dpr = window.devicePixelRatio;
		const backing = backingSize({ cssW, cssH, dpr, resizing: backingResizing });
		const visualInputs = [
			backing.width,
			backing.height,
			ghost.blinkPhase,
			deck.stable_id,
			deck.duration_ms,
			paintAnlz,
			deck.pitch,
			deck.loop,
			syncPlayheadTone,
			cssW,
			cssH,
			palette,
			bandGains,
			uiPrefs.show_sections,
			splitPartner?.id ?? null,
			partnerAnlz,
			partnerPaintMs,
			partnerState?.pitch ?? null,
			partnerGains,
			Math.round(view.windowS * 1000),
			Math.round(view.frac * cssW),
			planCurves,
			partnerPlanCurves,
			tempoSegments,
			partnerTempoSegments,
			tempoColors,
			tempoScale,
			hideTempo
		] as const;
		tracePlayhead('wave-clock', deckId, paintPositionMs); // before the sub-pixel skip: the clock itself
		if (shouldSkipRepaint(_paintScheduleState, force, visualInputs, scrollPx)) return;
		tracePlayhead('wave', deckId, paintPositionMs);
		if (splitPartner !== null && partnerPaintMs !== null) tracePlayhead('wave-partner', splitPartner.id, partnerPaintMs);
		if (el.width !== backing.width || el.height !== backing.height) {
			el.width = backing.width;
			el.height = backing.height;
			// While bucketed the element is the bucket size so the bitmap is never
			// squashed; .canvas-wrap clips it and painting stays in the top-left
			// cssW x cssH region. Settled, the stylesheet's 100% applies again.
			el.style.width = backing.bucketed ? `${backing.cssW}px` : '';
			el.style.height = backing.bucketed ? `${backing.cssH}px` : '';
		}
		const ctx = el.getContext('2d');
		if (ctx === null) throw new Error(`wavestack deck ${deckId}: 2d context unavailable`);
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		const paintPalette = resolvePaintPalette(deckId, palette);
		if (deck.stable_id === null || deck.duration_ms === null) {
			// Empty deck: flat dark row + state-derived center line - never an invented waveform.
			ctx.fillStyle = paintPalette.bg;
			ctx.fillRect(0, 0, cssW, cssH);
			drawPlayhead(ctx, cssW, cssH, syncPlayheadTone, 0, view.frac);
			return;
		}
		if (splitPartner !== null && partnerState !== null && partnerPaintMs !== null && partnerState.duration_ms !== null) {
			const half = Math.floor(cssH / 2);
			const common = { widthCss: cssW, heightCss: half, palette: paintPalette, showSections: uiPrefs.show_sections, waveformDesign: effectiveWaveformDesign(uiPrefs.waveform_design, uiPrefs.ui_skin), windowS: view.windowS, playheadFrac: view.frac, ...(view.cacheWindowS === undefined ? {} : { cacheWindowS: view.cacheWindowS }) };
			paintSplitRow(
				el,
				ctx,
				{
					...common,
					positionMs: partnerPaintMs,
					durationMs: partnerState.duration_ms,
					anlz: partnerAnlz,
					pitch: partnerState.pitch,
					loop: partnerState.loop,
					playheadTone: partnerState.is_master ? 'master' : partnerState.audible ? 'now' : 'stopped',
					playheadTimeMs: performance.now(),
					bandGains: partnerGains,
					planCurves: partnerPlanCurves,
					tempo: partnerTempoSegments,
					tempoColors,
					tempoScale,
					hideTempo
				},
				{
					...common,
					positionMs: paintPositionMs,
					durationMs: deck.duration_ms,
					anlz: paintAnlz,
					pitch: deck.pitch,
					loop: deck.loop,
					playheadTone: syncPlayheadTone,
					playheadTimeMs: performance.now(),
					ghostSeekMs: ghost.ghostSeekMs,
					ghostSeekVisible: ghost.ghostSeekVisible,
					bandGains,
					planCurves,
					tempo: tempoSegments,
					tempoColors,
					tempoScale,
					hideTempo
				},
				{ top: splitPartner.label, bottom: `DECK ${deckId}`, color: paintPalette.phrase, line: paintPalette.tick },
				dpr
			);
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
			showSections: uiPrefs.show_sections,
			waveformDesign: effectiveWaveformDesign(uiPrefs.waveform_design, uiPrefs.ui_skin),
			masterDownbeatOverlay,
			masterBeatGhost,
			ghostSeekMs: ghost.ghostSeekMs,
			ghostSeekVisible: ghost.ghostSeekVisible,
			bandGains,
			windowS: view.windowS,
			playheadFrac: view.frac,
			...(view.cacheWindowS === undefined ? {} : { cacheWindowS: view.cacheWindowS }),
			planCurves,
			tempo: tempoSegments,
			tempoColors,
			tempoScale,
			hideTempo
		});
	}

	// Is the painted position actually moving? deck.playing is desired INTENT
	// (audio-engine sets st.playing = rt.desiredActive), not presented truth, so
	// this loop will happily repaint a pixel-identical frame at 60Hz forever and
	// call it healthy - which is exactly what it did for twenty minutes on
	// Wed 2 Sep 2026. Nothing here can fix a frozen clock; what it can do is stop
	// lying about it. See (private path)/
	// waveform-freezes-on-stale-output-timestamp.md.
	let stallState = $state.raw<PresentationStallState | undefined>(undefined);
	let playheadFrozen = $state(false);
	// Either the device clock stalled (presentation.ts is coasting on the sample
	// clock) or the painted number itself stopped moving for any other reason.
	const clockUntrusted = $derived(playheadFrozen || isPresentationClockStalled(deckId));
	// A synced-but-not-master follower riding the master's motion, so it must
	// keep painting while `deck.playing` is false. Read by both effects below.
	const masterMoving = $derived(deck.beat_sync_enabled && !deck.is_master && (masterState?.playing ?? false));
	// Split row: the partner (top half) scrolls on its own clock, so keep
	// painting while it plays even when this deck is paused.
	const partnerMoving = $derived(splitPartner !== null && (partnerState?.playing ?? false));

	// The hover-only pulse the rAF loop below keeps running. Without hover nothing paints a paused follower on a frame
	// loop, so the stopped-deck repaint must keep it: a cue or a plan's curves on an idle synced deck have to show.
	const hoverPulse = $derived(deckHoverUi.deckId === deckId && (syncPlayheadTone === 'drift' || masterMoving));

	// rAF while playing/scrubbing, drift pulse, or master is moving under a synced follower.
	$effect(() => {
		// The hover and drift-pulse inputs are read ONLY when nothing else keeps the
		// loop running. Read unconditionally, a follower's sync tone flipping mid-frame
		// re-ran this effect, cancelled the frame already queued and dropped one
		// (ANIM-CLOCK-01: about one frame a second on the follower's strip).
		const running =
			deck.playing || seeking || waveformSeekArmed !== null || partnerMoving || zoomAnimActive || elasticFollowing || refMoving || refTempoLive;
		if (!running && !hoverPulse) return;
		let raf = requestAnimationFrame(function waveRowFrame(timestamp) {
			draw();
			if (zoomAnimActive || elasticFollowing || refMoving || refTempoLive) zoomFrame++;
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
		if (
			deck.playing || seeking || hoverPulse || partnerMoving ||
			zoomAnimActive || elasticFollowing || refMoving || refTempoLive
		) {
			return;
		}
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
		// Under a moving master this would repaint an idle follower on every engine tick.
		if (!masterMoving) void masterState?.position_ms;
		void partnerState?.position_ms;
		void partnerState?.pitch;
		void partnerAnlz;
		void splitPartner;
		void planCurves;
		void partnerPlanCurves;
		void tempoSegments;
		void partnerTempoSegments;
		void tempoColors;
		void tempoScale;
		void hideTempo;
		void effectiveWaveformDesign(uiPrefs.waveform_design, uiPrefs.ui_skin);
		void cssW;
		void cssH;
		void backingResizing;
		void zoomStep;
		void zoomRefDeck;
		// A paused reference deck that is scrubbed repaints every stopped row, so
		// the reference's live position is tracked here, not only its identity.
		void (zoomRefDeck === null ? null : getDeckState(zoomRefDeck.id).position_ms);
		void uiPrefs.wave_zoom_playhead;
		void uiPrefs.show_sections;
		// STRUCT-05: subscribe to the lazily loaded section painter so the
		// bands paint as soon as it lands after the pref turns on.
		void sectionsApiFor(uiPrefs.show_sections);
		void palette;
		// WAVE-EQ-5230: a paused deck repaints when its EQ or filter moves.
		void paintBandGains(deckId, uiPrefs.wave_eq_reflect);
		if (splitPartner !== null) void paintBandGains(splitPartner.id, uiPrefs.wave_eq_reflect);
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
			windowSeconds: _zoomView(scrubOriginPositionMs).windowS * deck.pitch
		});
		return snapWaveTargetMs(raw, scrubBeats, waveSnapModeFromModifiers(modifiers, scrubQuantize));
	}

	function _clickTarget(clientX: number, modifiers: { shiftKey: boolean; metaKey: boolean }): number {
		return waveClickSeekMs({
			centerPositionMs: scrubOriginPositionMs,
			pointerX: clientX - scrubLeftPx,
			widthPx: scrubWidthPx,
			durationMs: scrubDurationMs,
			...((view) => ({ windowSeconds: view.windowS * deck.pitch, playheadFrac: view.frac }))(
				_zoomView(scrubOriginPositionMs)
			),
			beats: scrubBeats,
			mode: waveSnapModeFromModifiers(modifiers, scrubQuantize)
		});
	}

	function _clearGesture(): void {
		scrubPointerId = null;
		scrubMoved = false;
		seeking = false;
		scrubFrozenWindowS = null;
		setWaveZoomScrubWindowS(deckId, null);
		_setScrubPreview(null);
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
		// The time painted under the playhead column, so a click maps through the
		// picture the DJ sees, not a published position that lags it while playing.
		scrubOriginPositionMs = _paintPositionMs();
		scrubDurationMs = deck.duration_ms;
		scrubLeftPx = rect.left;
		scrubWidthPx = rect.width;
		scrubMoved = false;
		scrubDispatchError = null;
		scrubFrozenWindowS = _zoomView(_paintPositionMs()).windowS;
		setWaveZoomScrubWindowS(deckId, scrubFrozenWindowS);
		seeking = true;
		// SPIKE-PERF: jump the painted window under the pointer immediately.
		_setScrubPreview(_clickTarget(event.clientX, event));
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
			// WAVE-SEEK-01: a click lands where it was made, like a CDJ needle
			// search; no deferred bar launch, no bar or phrase snap.
			const targetMs = scrubMoved
				? _dragTarget(event.clientX, event)
				: _clickTarget(event.clientX, event);
			_setScrubPreview(targetMs);
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
	class:deck-focus={deckHoverLinkId(uiPrefs.deck_hover_link) === deckId}
	data-deck={deckId} data-wave-surface="row"
	data-wave-window-s={laneView.windowS}
	data-wave-playhead-frac={laneView.frac}
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
	<WaveGutter {deck} {deckId} />
	<div class="wave-col">
		<div class="canvas-wrap" title={vocalsTitle ?? undefined} bind:this={canvasWrapEl}>
			{#if zoomChipHere}
				<!-- Keyed so each step remounts the chip and restarts its fade. -->
				{#key zoomStep}
					<span
						class="zoom-chip"
						title={`Waveform zoom: ${waveZoomLabel(zoomStep).toLowerCase()} across the row. Ctrl or Cmd plus the mouse wheel, a trackpad pinch, or the [ and ] keys change it.`}
					>{waveZoomLabel(zoomStep)}</span>
				{/key}
			{/if}
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
			{#if planCurves !== null}
				<span class="plan-chip" data-plan-chip={planCurves.role} title={planChipTitle(planCurves, planAnchorMoved())}>
					{planChipText(planCurves, planAnchorMoved())}
				</span>
			{/if}
			<LyricLanes stableId={deck.stable_id} lyrics={lyricsState.lyrics} loadError={lyricsState.loadError} positionMs={_paintPositionMs()} pitch={deck.pitch} windowSeconds={laneView.windowS} playheadFrac={laneView.frac} />
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
		<StemWaveStack {deck} {showStems} scrollPx={stemScrollPx} canvasWidth={cssW} windowS={laneView.windowS} playheadFrac={laneView.frac} />
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
	/* DECKUX-5917: the hover link no longer lifts the fill (WaveRow.chrome.css). */
	.rb-waverow.secondary.deck-focus {
		background: var(--rb-waverow-secondary);
	}
	/* WAVEZOOM-6802b: the zoom chip ("16 BARS") fades in on a step change and out
	   after 1.2 s; it says where you are without a permanent control. */
	.zoom-chip {
		position: absolute;
		top: 2px;
		right: 4px;
		z-index: 3;
		padding: 1px 5px;
		border-radius: 2px;
		background: color-mix(in srgb, var(--rb-bg) 70%, transparent);
		border: 1px solid var(--rb-border);
		color: var(--rb-text, #fff);
		font-size: 9px;
		font-weight: 700;
		letter-spacing: 0.08em;
		font-variant-numeric: tabular-nums;
		pointer-events: none;
		animation: zoom-chip-fade 1200ms ease-out forwards;
	}
	@keyframes zoom-chip-fade {
		0% { opacity: 0; }
		10% { opacity: 1; }
		75% { opacity: 1; }
		100% { opacity: 0; }
	}
	@media (prefers-reduced-motion: reduce) {
		.zoom-chip { animation: none; }
	}
</style>
