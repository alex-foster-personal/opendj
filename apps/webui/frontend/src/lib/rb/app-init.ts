/**
 * Instruments that must run exactly once per page load.
 *
 * These live here rather than inline in +layout.svelte so they are unit
 * testable. That is not decoration: installPerfEventLogGlobal() was
 * exported and never called by anything in production, so
 * window.__mdtPerfLog did not exist at runtime and the e2e latency floor
 * in tests/e2e/performance-controls.spec.ts failed with "the latency
 * instrument is missing". A unit test on the installer could not catch
 * that, because the installer was fine; nobody ran it. A test on THIS
 * module can, and does.
 */

import { bootScheduler, type BootScheduler } from './boot-scheduler';
import { applyExplicitPerfTierPref, fetchPerfTier } from './perf-tier-client';
import {
	pressureIsElevated,
	readMachinePressure,
	startMachinePressurePolling,
	subscribeMachinePressure
} from './machine-pressure';
import { installPerfEventLogGlobal } from './perf-event-log';
import {
	armCloudsyncSchedulerShed,
	resumeCloudsyncSchedulerOwedJob
} from './cloudsync-scheduler-shed';
import { anyDeckPlaying, startBackgroundDemandShed } from './playing-gate';
import { applyPreviewCaps } from '$lib/player/preview-cue.svelte';
import { applyPrefetchCaps, resumeAudioPrefetchOwedPump, setAudioPrefetchShedRequest } from './audio-prefetch-cache.svelte';
import { armPrefetchPressureCapScaling } from './prefetch-pressure-caps';
import { resumeEagerStemDecodeOwedJob, setEagerStemDecodeShed } from './stem-decode-shed';
import { resumeAnlzPrefetchOwedFetch, setAnlzPrefetchShedRequest } from '$lib/components/rb/wave/anlz-cache.svelte';
import { installReloadCountdown } from './reload-countdown';
import { readXrunSessionCounter } from './xrun-sentinel';
import { pushToast } from '$lib/stores.svelte';
import { startClientPerformanceSampling } from './client-performance-samples';
import { startUsageHeartbeat } from './usage-heartbeat';
import { DECK_IDS, deckStates, pitchRanges } from '$lib/rb/audio-engine.svelte';
import { getAutoPlayPlaylist, pickNextStableId, tempoBoundsFromPitchRange } from '$lib/rb/auto-play';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import {
	setSilenceDropoutContextReader,
	setSilenceDropoutHandler
} from '$lib/rb/master-silence-report';
import { readAutoPlayHandoffInFlight } from '$lib/rb/auto-play.svelte';
import { setUnexpectedPauseAutoPlayReader } from '$lib/rb/unexpected-pause-report';
import { handleSilenceDropoutPlan } from '$lib/rb/silence-dropout-act';
import type { SilenceDropoutDeckSnap } from '$lib/rb/silence-dropout';
import type { DeckId } from '$lib/rb/deck-slots';

function _readSilenceDropoutDecks(): readonly SilenceDropoutDeckSnap[] {
	return DECK_IDS.map((id: DeckId) => {
		const deck = deckStates[id];
		return {
			id,
			playing: deck.playing,
			audible: deck.audible,
			bpm: deck.bpm,
			beat_sync_enabled: deck.beat_sync_enabled,
			sync_mode: deck.sync_mode,
			sync_error: deck.sync_error,
			processor_error: deck.processor_error,
			is_master: deck.is_master
		};
	});
}

function _hasPlayableAutoPlayNext(): boolean {
	if (!uiPrefs.auto_play_enabled) return false;
	const master = DECK_IDS.find((id) => deckStates[id].is_master && deckStates[id].stable_id !== null);
	if (master === undefined) return false;
	const follower = DECK_IDS.find((id) => id !== master && deckStates[id].stable_id === null);
	const bounds = tempoBoundsFromPitchRange(pitchRanges[follower ?? master]);
	const source = deckStates[master];
	if (source.stable_id === null) return false;
	return (
		pickNextStableId({
			playlist: getAutoPlayPlaylist(),
			current_stable_id: source.stable_id,
			current_key: source.key,
			current_bpm: source.bpm,
			exclude_ids: new Set<string>(),
			played_ids: new Set<string>(),
			enforce_play_order: uiPrefs.auto_play_enforce_order,
			min_tempo_ratio: bounds.min,
			max_tempo_ratio: bounds.max,
			maximize_reach: !uiPrefs.auto_play_enforce_order && uiPrefs.auto_play_maximize_reach
		}) !== null
	);
}

let _xrunsAtPrevious = 0;

/**
 * Start the page-lifetime instruments. Returns the teardown, which the
 * caller owns (the root layout hands it back from onMount).
 *
 * The scheduler is a parameter so the unit suite can drive the boot window
 * by hand instead of waiting BOOT_QUIET_MS of real time. Production passes
 * nothing and gets the page's one scheduler.
 */
export function startAppInstruments(scheduler: BootScheduler = bootScheduler): () => void {
	installPerfEventLogGlobal();
	if (uiPrefs.perf_tier !== 'auto') {
		applyExplicitPerfTierPref(uiPrefs.perf_tier);
	}
	scheduler.defer('perf-tier:fetch', () => fetchPerfTier());
	const stopBootScheduler = scheduler.start();
	const stopUsageHeartbeat = startUsageHeartbeat(scheduler);
	const stopReloadCountdown = installReloadCountdown();
	const stopMachinePressurePolling = startMachinePressurePolling(scheduler);
	const stopClientPerformanceSampling = startClientPerformanceSampling(scheduler);
	let stopCloudsyncSchedulerShed: (() => void) | undefined;
	const stopBackgroundDemandShed = startBackgroundDemandShed({
		isPlaying: anyDeckPlaying,
		pressureElevated: () => pressureIsElevated(readMachinePressure()),
		readXruns: () => readXrunSessionCounter().xruns,
		notify: (suggestion) => pushToast(suggestion.message, 'warn'),
		jobs: [
			{ id: 'cloudsync-scheduler', run: resumeCloudsyncSchedulerOwedJob },
			{ id: 'audio-prefetch-cache-caps', run: resumeAudioPrefetchOwedPump },
			{ id: 'eager-stem-decode', run: resumeEagerStemDecodeOwedJob },
			{ id: 'waveform-detail-bands', run: resumeAnlzPrefetchOwedFetch }
		],
		onShed: (shed) => {
			stopCloudsyncSchedulerShed = armCloudsyncSchedulerShed(
				shed,
				() => readXrunSessionCounter().xruns
			);
			setAudioPrefetchShedRequest((id) => shed.request(id));
			setEagerStemDecodeShed(shed);
			setAnlzPrefetchShedRequest((id) => shed.request(id));
		}
	});
	const stopPrefetchPressureCapScaling = armPrefetchPressureCapScaling({
		isPlaying: anyDeckPlaying,
		pressureElevated: () => pressureIsElevated(readMachinePressure()),
		readXruns: () => readXrunSessionCounter().xruns,
		// CUEOUT-15: the preview's decoded-audio budget rides the same closed
		// loop, so a pressure step evicts previewed PCM at the same moment it
		// shrinks the prefetch caps.
		applyCaps: () => {
			applyPrefetchCaps();
			applyPreviewCaps();
		},
		subscribe: subscribeMachinePressure
	});
	_xrunsAtPrevious = readXrunSessionCounter().xruns;
	setSilenceDropoutHandler(handleSilenceDropoutPlan);
	setUnexpectedPauseAutoPlayReader(() => ({
		armed: uiPrefs.auto_play_enabled,
		handoff_in_flight: readAutoPlayHandoffInFlight()
	}));
	setSilenceDropoutContextReader(() => {
		const xruns = readXrunSessionCounter().xruns;
		const ctx = {
			decks: _readSilenceDropoutDecks(),
			autoplay_enabled: uiPrefs.auto_play_enabled,
			has_playable_next: _hasPlayableAutoPlayNext(),
			xruns,
			xruns_at_previous: _xrunsAtPrevious
		};
		_xrunsAtPrevious = xruns;
		return ctx;
	});

	return () => {
		setSilenceDropoutHandler(null);
		setUnexpectedPauseAutoPlayReader(null);
		setSilenceDropoutContextReader(null);
		setAudioPrefetchShedRequest(null);
		setEagerStemDecodeShed(null);
		setAnlzPrefetchShedRequest(null);
		stopPrefetchPressureCapScaling();
		stopCloudsyncSchedulerShed?.();
		stopBackgroundDemandShed();
		stopMachinePressurePolling();
		stopClientPerformanceSampling();
		stopReloadCountdown();
		stopUsageHeartbeat();
		stopBootScheduler();
	};
}
