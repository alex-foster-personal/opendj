/** Push a human-readable projection of the live performance screen to engine. */
import { toasts } from '$lib/stores.svelte';
import { audioContextState } from './audio-engine.svelte';
import { masterSilenceState, outputDeviceLivenessState } from './master-silence-report';
import { readAutoPlayStall } from './autoplay-stall.svelte';
import { readAutoPlayMirrorStatus } from './auto-play.svelte';
import { readLastDeckStop } from './deck-stop-log';
import type { DeckId } from './deck-slots';
import { queryPerformanceState } from './performance-ipc.svelte';
import { installAgentOrderPoll } from './agent-orders';
import type { TabLeadership } from './tab-leadership';
import { createLeasedMirrorPublisher, MIRROR_PATH } from './leased-mirror-publisher';
import { readXrunSessionCounter } from './xrun-sentinel';
import { audioOutputHealth } from '$lib/rb/audio-output-health.svelte';
import { outputTopologyMirror } from '$lib/rb/audio-output-status.svelte';
import { readPerfEvents, recordPerfEvent } from './perf-event-log';
import { buildAudioHealthMirror } from './audio-health-mirror';
import {
	classifyMirrorPublishGap,
	mirrorStallMessage
} from './mirror-publish-stall';
import { countVisibleTrackRows } from './track-row-visibility';
import { buildControlsMap, CONTROL_SELECTOR, controlPreferredName } from './ui-mirror-controls';

/** CUEOUT-18: one id per page load, so the engine can keep two open tabs'
 * headphone reports apart. Not the Web Crypto UUID call: that needs a
 * secure context, and the id only has to differ between tabs on one machine. */
/** AGENT-20: matches UI_MIRROR_SCHEMA in apps/webui/server/routes/state.py. */
export const UI_MIRROR_SCHEMA = 2;

const MIRROR_CLIENT_ID = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

/** `buildUiMirror` runs inside `window.setInterval`, so an uncaught throw
 * here would abort the whole publish - every sibling field (decks, audio
 * health, toasts, the agent-order poll gate) goes dark, not just this one
 * integer, and every `/api/v1/commands` route starts answering 409 (see
 * `installUiMirror` below). `countVisibleTrackRows` can throw if
 * TrackTable's wrapper markup ever moves, so its failure is caged here and
 * degraded to 0 for this field alone, with a durable, non-dismissing
 * `recordPerfEvent` entry standing in for the toast that would otherwise
 * vanish in five seconds. */
function _visibleRowsCount(): number {
	try {
		return countVisibleTrackRows();
	} catch (error) {
		recordPerfEvent(
			'ui-mirror-visible-rows',
			error instanceof Error ? error.message : String(error),
			null,
			'error'
		);
		return 0;
	}
}

function _controls(): Record<string, 'available' | 'inert'> {
	return buildControlsMap(document.querySelectorAll(CONTROL_SELECTOR));
}

function _position(deck: ReturnType<typeof queryPerformanceState>['decks'][1]): {
	ms: number;
	bars_beats: string | null;
	phrase: number | null;
} {
	const beat = [...deck.beatgrid].reverse().find((row) => row.time_ms <= deck.position_ms);
	const phrase = deck.phrases.findIndex((row, index) =>
		row.start_ms <= deck.position_ms && (index === deck.phrases.length - 1 || deck.phrases[index + 1].start_ms > deck.position_ms)
	);
	if (beat === undefined) return { ms: deck.position_ms, bars_beats: null, phrase: phrase < 0 ? null : phrase + 1 };
	return {
		ms: deck.position_ms,
		bars_beats: `${Math.floor((beat.n - 1) / 4) + 1}.${((beat.n - 1) % 4) + 1}`,
		phrase: phrase < 0 ? null : phrase + 1
	};
}

export function buildUiMirror(): Record<string, unknown> {
	const state = queryPerformanceState();
	const silence = masterSilenceState();
	// Two distinct claims (#1642): the mixer being quiet (silence.verdict) and
	// the room hearing nothing despite a fine mixer (deviceLiveness.verdict).
	// Both gate `audible`, and both get their own toast id, so a device-level
	// outage never collapses into "the mixer is quiet" in the UI mirror.
	const deviceLiveness = outputDeviceLivenessState();
	const autoPlay = readAutoPlayMirrorStatus();
	return {
		client_open: true,
		client_id: MIRROR_CLIENT_ID,
		// AGENT-20: the document version. From 2 the engine refuses a publish
		// missing the autoplay_* fields; an unversioned (older) page is still
		// accepted during the release, with a logged warning and no default.
		mirror_schema: UI_MIRROR_SCHEMA,
		// AGENT-18: the engine hands the lease away from a hidden, silent tab.
		tab: { visible: document.visibilityState === 'visible' },
		published_at: new Date().toISOString(),
		// The elected master, and so the deck a Duration times against when
		// no clock is named. Without it an agent cannot resolve its own
		// beat-relative order against the grid the page will use (#1739).
		master_deck: state.master_deck,
		master_mode: state.master_mode,
		master_reason: state.master_reason,
		transition: state.transition,
		context_state: audioContextState(),
		// IOPIN-12 parity with the I/O panel notice: what djio asked for, what the
		// graph actually wired, and the stereo-fallback reason when they differ.
		output_topology: outputTopologyMirror(),
		master: { ...state.master, level: state.mixer.master, rms: silence.rms },
		xrun_sentinel: readXrunSessionCounter(),
		mixer: state.mixer,
		decks: Object.fromEntries(
			Object.entries(state.decks).map(([id, deck]) => [id, {
				// stable_id rides with title because title alone cannot answer WHICH
				// track landed: a load onto an already-loaded deck leaves a title
				// present either way, so an agent confirming a load off title
				// confirms the PREVIOUS track (#1739).
				stable_id: deck.stable_id,
				title: deck.title, artist: deck.artist, key: deck.key, bpm: deck.bpm,
				effective_bpm: deck.effective_bpm, position: _position(deck), playing: deck.playing,
				quantized_launch_armed: deck.quantized_launch_armed,
				audible:
					deck.audible &&
					silence.verdict !== 'silent-while-playing' &&
					silence.verdict !== 'output-stalled-while-rendering' &&
					deviceLiveness.verdict !== 'device-unreachable',
				presentation_clock: { trust: deck.transport_clock.source === 'audio_output' && deck.transport_clock.desired_revision === deck.transport_clock.presented_revision ? 'trusted' : 'untrusted', ...deck.transport_clock },
				loop: deck.loop, hot_cues: deck.hot_cue_slots, pitch: deck.pitch,
				sync: { mode: deck.sync_mode, enabled: deck.beat_sync_enabled }, stems: deck.stems,
				phrases: deck.phrases,
				// PLAY-18: the deck's last stop and its cause; user_pause is true only
				// when a person stopped it. The engine logs each new one on ingest.
				last_stop: readLastDeckStop(Number(id) as DeckId)
			}])
		),
		browser: {
			playlist: state.browser.active_playlist,
			search: state.browser.search,
			sort: state.browser.sort,
			selected_row: state.browser.selected_row,
			visible_rows_count: _visibleRowsCount(),
			// Observe `browser.load.complete` to wait for a whole list.
			load: state.browser.load
		},
		toasts: [
			...toasts.map((toast) => ({ id: toast.logId, kind: toast.kind, message: toast.message })),
			...(silence.verdict === 'silent-while-playing' ? [{ id: 'silent-while-playing' }] : []),
			...(silence.verdict === 'output-stalled-while-rendering'
				? [{ id: 'output-stalled-while-rendering' }]
				: []),
			...(deviceLiveness.verdict === 'device-unreachable' ? [{ id: 'output-device-unreachable' }] : [])
		],
		// AGENT-02 parity for audio health, and the durable half of the toast
		// problem below. `toasts` above is the LIVE store: entries dismiss after
		// about five seconds while this publishes every second, so a fault is
		// unreadable moments later. On Thu 10 Sep 2026 the operator watched
		// several audio errors on screen while this document published
		// `toasts: []`, and the output-health bar he could see at
		// `TopBar.svelte:605` was mirrored nowhere at all. `audio_health` carries
		// that same bar's reading plus the DURABLE fault ring, so the next
		// outage is still legible to an agent long after the toasts are gone.
		audio_health: buildAudioHealthMirror({
			snapshot: audioOutputHealth.snapshot,
			rms: silence.rms,
			rmsAgeMs: silence.at_ms === null ? null : Date.now() - silence.at_ms,
			silenceVerdict: silence.verdict,
			events: readPerfEvents(),
			nowMs: Date.now()
		}),
		// PLAY-08: agent-native parity for the stall banner. An agent driving a
		// set reads why AutoPlay stopped from the same object a person reads
		// off the screen, rather than having to catch a five-second toast.
		autoplay_stall: readAutoPlayStall(),
		// AGENT-20: whether AutoPlay is on, and whether it would hand off right
		// now (with the reason when it would not). The server requires these
		// three from any publisher declaring mirror_schema 2.
		autoplay_enabled: autoPlay.autoplay_enabled,
		autoplay_armed: autoPlay.autoplay_armed,
		autoplay_disarm_reason: autoPlay.autoplay_disarm_reason,
		open_overlays: [...document.querySelectorAll('[role="dialog"], .overlay, .modal')].map((element, index) =>
			controlPreferredName(element, index)
		),
		controls: _controls()
	};
}

function _anyDeckPlaying(): boolean {
	return Object.values(queryPerformanceState().decks).some((deck) => deck.playing);
}

export function installUiMirror(leadership: TabLeadership): () => void {
	// The engine only knows a performance page is open once it has ACCEPTED a
	// mirror publish, and every /api/v1/commands route answers 409 until then.
	// `mirror.isRegistered()` is that precondition, read by the order poll:
	// without it the poll races its own first publish, loses, and the browser
	// logs the 409 as a console error that no catch block can take back.
	//
	// AGENT-18: only the LEADER tab publishes or claims orders. A follower is
	// silent toward the engine, so it can neither overwrite the playing tab's
	// state nor execute an order the operator's tab should run.
	let lastPublishAtMs: number | null = null;
	const mirror = createLeasedMirrorPublisher({
		leadership,
		clientId: MIRROR_CLIENT_ID,
		build: buildUiMirror,
		isPlaying: _anyDeckPlaying,
		isVisible: () => document.visibilityState === 'visible',
		beforePublish: (nowMs) => {
			if (
				lastPublishAtMs !== null &&
				classifyMirrorPublishGap(nowMs - lastPublishAtMs) === 'stall'
			) {
				recordPerfEvent(
					'mirror-stall',
					mirrorStallMessage(nowMs - lastPublishAtMs),
					null,
					'error'
				);
			}
			lastPublishAtMs = nowMs;
		}
	});
	let lastRole = leadership.snapshot().role;
	const unsubscribeLeadership = leadership.subscribe((snapshot) => {
		// Publish at once on BECOMING leader only; a confirmation notice is not a new role.
		if (snapshot.role === 'leader' && lastRole !== 'leader') mirror.publish();
		lastRole = snapshot.role;
	});
	mirror.publish();
	const interval = window.setInterval(mirror.publish, 1000);
	// The operator's latest touch makes this the operator's tab (see the claim rules).
	const noteGesture = (): void => mirror.noteGesture();
	window.addEventListener('pointerdown', noteGesture, { capture: true, passive: true });
	window.addEventListener('keydown', noteGesture, { capture: true, passive: true });
	const uninstallOrderPoll = installAgentOrderPoll(mirror, mirror.publish);
	// A follower never published, so it has nothing to close; deleting would
	// blank the leader's live mirror under it.
	const closeMirrorIfLeader = (): void => {
		if (!leadership.isLeader()) return;
		void fetch(MIRROR_PATH, {
			method: 'DELETE',
			keepalive: true,
			headers: { 'x-opendj-client-id': MIRROR_CLIENT_ID }
		}).catch(() => {});
	};
	// Closing a TAB never runs the route's unmount, so without this the lease
	// outlives the closed leader by up to its 10 s TTL and the next tab waits.
	window.addEventListener('pagehide', closeMirrorIfLeader);
	return () => {
		// Order matters: drop the registration and stop the poll BEFORE the mirror
		// is deleted, so teardown never leaves a poll asking about a page the
		// engine has just been told is gone.
		window.removeEventListener('pagehide', closeMirrorIfLeader);
		window.removeEventListener('pointerdown', noteGesture, { capture: true });
		window.removeEventListener('keydown', noteGesture, { capture: true });
		mirror.forget();
		uninstallOrderPoll();
		window.clearInterval(interval);
		closeMirrorIfLeader();
		unsubscribeLeadership();
	};
}
