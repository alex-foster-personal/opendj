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
import { setLiveTransportProbe } from '$lib/client-error-reporting';
import { resumeAudioPrefetchOwedPump, setAudioPrefetchShedRequest } from './audio-prefetch-cache.svelte';
import { applyAllCaps } from '$lib/rb/cache-caps-registry';
import { armPrefetchPressureCapScaling } from './prefetch-pressure-caps';
import { resumeEagerStemDecodeOwedJob, setEagerStemDecodeShed } from './stem-decode-shed';
import { resumeAnlzPrefetchOwedFetch, setAnlzPrefetchShedRequest } from '$lib/components/rb/wave/anlz-cache.svelte';
import { installReloadCountdown } from './reload-countdown';
import { readXrunSessionCounter } from './xrun-sentinel';
import { pushToast } from '$lib/stores.svelte';
import { startClientPerformanceSampling } from './client-performance-samples';
import { startUsageHeartbeat } from './usage-heartbeat';
import {
	DECK_IDS,
	deckAudioClockPositionMs,
	deckMixBuffer,
	deckStates,
	mixerState,
	pitchRanges
} from '$lib/rb/audio-engine.svelte';
import { onAirGain } from '$lib/rb/master-election';
import { everyStemPartSilent } from '$lib/sets/deck-audibility';
import { getAutoPlayPlaylist, pickNextStableId, tempoBoundsFromPitchRange } from '$lib/rb/auto-play';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import {
	setSilenceDropoutContextReader,
	setSilenceDropoutHandler,
	setSilenceSourceReader
} from '$lib/rb/master-silence-report';
import { readAutoPlayHandoffInFlight } from '$lib/rb/auto-play.svelte';
import { setUnexpectedPauseAutoPlayReader } from '$lib/rb/unexpected-pause-report';
import { handleSilenceDropoutPlan } from '$lib/rb/silence-dropout-act';
import type { SilenceDropoutDeckSnap } from '$lib/rb/silence-dropout';
import type { DeckId } from '$lib/rb/deck-slots';
import type { SilenceSourceDeckSnap } from '$lib/rb/silence-source-pcm';

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

/** Linear gain from a deck to the master analyser tap: the mixer chain, or 0
 * when a ready stem bundle has every part gained to zero. Same laws the graph
 * applies (`onAirGain`, `everyStemPartSilent`); EQ is left out because a full
 * cut is a dB threshold, not a zero, so an EQ-killed deck still reads as open. */
function _silenceMasterPathGain(id: DeckId): number {
	if (everyStemPartSilent(deckStates[id])) return 0;
	const ch = mixerState.channels[id];
	return onAirGain(
		{
			id,
			loaded: true,
			playing: true,
			beat_sync_enabled: false,
			fader: ch.fader,
			trim: ch.trim,
			assign: ch.assign
		},
		mixerState.crossfader,
		mixerState.master
	);
}

/** Source PCM at each deck's audio-clock playhead and its gain to the master
 * bus, for the master silence watchdog's source gate (issue #4030). Called only
 * on master-quiet samples. */
function readSilenceSourceDeckSnaps(): readonly SilenceSourceDeckSnap[] {
	return DECK_IDS.map((id: DeckId) => ({
		claims_live: deckStates[id].playing || deckStates[id].audible,
		buffer: deckMixBuffer(id),
		position_sec: deckAudioClockPositionMs(id) / 1000,
		master_path_gain: _silenceMasterPathGain(id)
	}));
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
	// Every client error from here on carries the page's own transport read,
	// so the engine can hold the Sentry forward while a deck is live. Wired
	// here rather than in client-error-reporting because that module boots
	// before the audio engine and must not import it.
	setLiveTransportProbe(anyDeckPlaying);
	if (uiPrefs.perf_tier !== 'auto') {
		applyExplicitPerfTierPref(uiPrefs.perf_tier);
	}
	scheduler.defer('perf-tier:fetch', () => fetchPerfTier());
	// Diagnostics consent (OBS-05) and, after acceptance, session replay
	// (OBS-06). Deferred like every other boot request, and gated on the same
	// live-transport read as error reporting so a replay never records a mix.
	// The module is imported inside the deferred task on purpose: it is not
	// on the first-paint path, and a static import would charge it (and the
	// dialog) to the library page's bundle budget.
	let stopTelemetryConsent: (() => void) | null = null;
	scheduler.defer('telemetry-consent:fetch', () =>
		Promise.all([
			import('$lib/telemetry-consent'),
			import('$lib/rb/live-transport-watch.svelte')
		]).then(([consent, watch]) => {
			stopTelemetryConsent = consent.bootTelemetryConsent({
				isLive: anyDeckPlaying,
				// Stops a replay in the microtask a deck goes live, ahead of any
				// flush timer; the poll inside is only the fallback.
				watchLive: watch.watchLiveTransport
			});
		})
	);
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
		// Every registered cache re-evicts on each pressure step, so a cache
		// added later cannot miss the closed loop.
		applyCaps: applyAllCaps,
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
	setSilenceSourceReader(readSilenceSourceDeckSnaps);

	return () => {
		stopTelemetryConsent?.();
		setLiveTransportProbe(null);
		setSilenceDropoutHandler(null);
		setUnexpectedPauseAutoPlayReader(null);
		setSilenceDropoutContextReader(null);
		setSilenceSourceReader(null);
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
