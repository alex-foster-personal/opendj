/** Push a human-readable projection of the live performance screen to engine. */
import { toasts } from '$lib/stores.svelte';
import { audioContextState } from './audio-engine.svelte';
import { masterSilenceState } from './master-silence-report';
import { queryPerformanceState } from './performance-ipc.svelte';
import { installAgentOrderPoll } from './agent-orders';
import { readXrunSessionCounter } from './xrun-sentinel';

const MIRROR_PATH = '/api/v1/state/ui-mirror';

function _controlName(element: Element, index: number): string {
	return (
		element.getAttribute('data-testid') ??
		element.getAttribute('aria-label') ??
		(element.textContent?.trim() || `control-${index + 1}`)
	);
}

function _controls(): Record<string, 'available' | 'inert'> {
	const controls: Record<string, 'available' | 'inert'> = {};
	document.querySelectorAll('button, input, [role="button"], [role="slider"]').forEach((element, index) => {
		controls[_controlName(element, index)] = element.classList.contains('rb-inert')
			? 'inert'
			: 'available';
	});
	return controls;
}

function _position(deck: ReturnType<typeof queryPerformanceState>['decks'][1]): {
	ms: number;
	bars_beats: string | null;
	phrase: number | null;
} {
	const beat = [...deck.beatgrid].reverse().find((row) => row.time_ms <= deck.position_ms);
	if (beat === undefined) return { ms: deck.position_ms, bars_beats: null, phrase: null };
	return {
		ms: deck.position_ms,
		bars_beats: `${Math.floor((beat.n - 1) / 4) + 1}.${((beat.n - 1) % 4) + 1}`,
		phrase: Math.floor((beat.n - 1) / 32) + 1
	};
}

export function buildUiMirror(): Record<string, unknown> {
	const state = queryPerformanceState();
	const silence = masterSilenceState();
	return {
		client_open: true,
		context_state: audioContextState(),
		master: { ...state.master, level: state.mixer.master, rms: silence.rms },
		xrun_sentinel: readXrunSessionCounter(),
		mixer: state.mixer,
		decks: Object.fromEntries(
			Object.entries(state.decks).map(([id, deck]) => [id, {
				title: deck.title, artist: deck.artist, key: deck.key, bpm: deck.bpm,
				effective_bpm: deck.effective_bpm, position: _position(deck), playing: deck.playing,
				audible: deck.audible && silence.verdict !== 'silent-while-playing',
				presentation_clock: { trust: deck.transport_clock.source === 'audio_output' && deck.transport_clock.desired_revision === deck.transport_clock.presented_revision ? 'trusted' : 'untrusted', ...deck.transport_clock },
				loop: deck.loop, hot_cues: deck.hot_cue_slots, pitch: deck.pitch,
				sync: { mode: deck.sync_mode, enabled: deck.beat_sync_enabled }, stems: deck.stems
			}])
		),
		browser: { playlist: state.browser.active_playlist, search: null, sort: null, selected_row: null, visible_rows_count: document.querySelectorAll('.track-row, [role="row"]').length },
		toasts: [
			...toasts.map((toast) => ({ id: toast.logId, kind: toast.kind, message: toast.message })),
			...(silence.verdict === 'silent-while-playing' ? [{ id: 'silent-while-playing' }] : [])
		],
		open_overlays: [...document.querySelectorAll('[role="dialog"], .overlay, .modal')].map((element, index) => _controlName(element, index)),
		controls: _controls()
	};
}

export function installUiMirror(): () => void {
	// The engine only knows a performance page is open once it has ACCEPTED a
	// mirror publish, and every /api/v1/commands route answers 409 until then.
	// This flag is that precondition, read by the order poll: without it the
	// poll races its own first publish, loses, and the browser logs the 409 as a
	// console error that no catch block can take back.
	let registered = false;
	const publish = (): void => {
		void fetch(MIRROR_PATH, {
			method: 'PUT',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify(buildUiMirror())
		}).then((response) => {
			registered = response.ok;
		});
	};
	publish();
	const interval = window.setInterval(publish, 1000);
	const uninstallOrderPoll = installAgentOrderPoll(
		{
			isRegistered: () => registered,
			forget: () => {
				registered = false;
			}
		},
		publish
	);
	return () => {
		// Order matters: drop the registration and stop the poll BEFORE the mirror
		// is deleted, so teardown never leaves a poll asking about a page the
		// engine has just been told is gone.
		registered = false;
		uninstallOrderPoll();
		window.clearInterval(interval);
		void fetch(MIRROR_PATH, { method: 'DELETE', keepalive: true });
	};
}
