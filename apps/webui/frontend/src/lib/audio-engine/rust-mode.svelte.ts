/**
 * Rust engine mode: the performance page plays through `odj-audio` instead of
 * the in-page Web Audio engine (GSD phase 20, toward switch-over 20-07).
 *
 * Opt-in and off by default. Turn it on with `?engine=rust` (remembered in
 * this browser) and off with `?engine=webaudio`. `?engine_clock=wall` runs the
 * engine without an output device, for CI and headless boxes; the default is
 * `device`.
 *
 * How it fits without rebuilding the page:
 *
 * - Every performance control already goes through the typed dispatcher in
 *   `performance-ipc.svelte.ts`. In this mode `_execute` hands audio commands
 *   to `executeInRustEngine` first. The engine speaks the same command names in
 *   the same 0..1 units, so most commands are forwarded as they are.
 * - The UI keeps reading the same stores (`deckStates`, `mixerState`). This
 *   module writes them: track, waveform, cues and grid from the same fetches
 *   the Web Audio load uses; playhead, play state, tempo, cue and loop from the
 *   engine's state feed; knob positions from each acknowledged command.
 * - `load` goes through `POST /api/v1/audio-engine/load`, so the engine gets
 *   the file and the beatgrid the waveform draws (own or rekordbox, per the
 *   analysis-source selection).
 *
 * What the Rust engine does not do yet is refused, never faked: its own
 * commands it has not built (sync, keylock, stems, hot-cue triggers, the
 * headphone cue bus) come back `not_implemented` naming the plan that brings
 * them, and page features that are bound to Web Audio internals throw
 * `RUST_ENGINE_UNAVAILABLE` naming the command. Everything else (library,
 * panels, feedback) runs on the page as before.
 */

import { API_BASE } from '$lib/api/base';
import { getTrack } from '$lib/api';
import { fetchAnlzForDeckLoad } from '$lib/components/rb/wave/anlz-cache.svelte';
import {
	_hotCueRevisionsFrom,
	deckStates,
	mixerState,
	pitchRanges
} from '$lib/player/state.svelte';
import { fetchHotCueSlots } from '$lib/rb/api-rb';
import { displayLoopFrom } from '$lib/rb/beat-sync-math';
import type { DeckId } from '$lib/rb/deck-slots';
import type { DeckState } from '$lib/rb/deck-state-types';
import { hotCuesFromAnlz } from '$lib/rb/hot-cue-from-anlz';
import type { PerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import {
	AUDIO_ENGINE_PATH,
	AudioEngineClient,
	type AudioEngineStatus,
	type EngineState
} from './client';

export const ENGINE_PREF_KEY = 'odj.audioEngine';

/** Commands the engine owns, built or not. Built ones play; the rest come
 * back `not_implemented` from the engine with the plan that brings them. */
export const ENGINE_COMMANDS: ReadonlySet<string> = new Set([
	'load',
	'unload',
	'play',
	'cue',
	'seek',
	'loop',
	'beat_loop',
	'beat_jump',
	'tempo',
	'pitch_range',
	'master_tempo',
	'trim',
	'eq',
	'filter',
	'fader',
	'assign',
	'crossfader',
	'master_volume',
	'master_mute',
	// Engine commands for later plans (protocol.rs LATER).
	'key_nudge',
	'key_sync',
	'stem_mute',
	'stem_solo',
	'stem_gain',
	'stem_eq_mode',
	'slip',
	'beat_sync',
	'sync_mode',
	'master',
	'quantize',
	'quantize_grid',
	'hot_cue_trigger',
	'channel_cue',
	'headphone_mix',
	'headphone_level',
	'head_delay_ms',
	'output_mode',
	'headphone_output_select',
	'headphone_master_select',
	'preview_cue',
	'preview_stop'
]);

/** Page features whose implementation reaches into the Web Audio engine's
 * own clock, buffers or devices. Refused by name in this mode. */
export const WEB_AUDIO_ONLY: ReadonlySet<string> = new Set([
	'load_play_intent',
	'loop_interval_mode',
	'loop_interval_base',
	'safety_loop_save',
	'safety_loop_arm',
	'safety_loop_clear',
	'hot_cue_save',
	'hot_cue_clear',
	'hot_cue_restore',
	'headphone_outputs_refresh',
	'headphone_output_acquire',
	'headphone_input_select',
	'headphone_calibrate',
	'headphone_calibrate_abort',
	'headphone_alignment_mode',
	'master_delay_ms',
	'auto_play_two_track',
	'auto_play_next_arm',
	'auto_play_next_cancel',
	'rescue_resume',
	'rescue_stop_all',
	'pairing_snapshot_save'
]);

export type EngineChoice = 'webaudio' | 'rust';

export const rustMode = $state({
	enabled: false,
	clock: 'device' as 'device' | 'wall',
	status: 'off' as 'off' | 'starting' | 'connected' | 'error',
	error: null as string | null
});

/** Read the choice from `?engine=` (remembered) or the remembered value. */
export function readEngineChoice(search: string, stored: string | null): EngineChoice {
	const q = new URLSearchParams(search).get('engine');
	const v = q ?? stored;
	if (v === null || v === 'webaudio') return 'webaudio';
	if (v === 'rust') return 'rust';
	throw new Error(`engine=${v} is not one of webaudio, rust`);
}

function _stored(): string | null {
	try {
		return window.localStorage?.getItem(ENGINE_PREF_KEY) ?? null;
	} catch {
		// Storage can be blocked; the URL still works.
		return null;
	}
}

/** The engine the performance page uses on its next load (the setting). */
export function storedEngineChoice(): EngineChoice {
	if (typeof window === 'undefined') return 'webaudio';
	return readEngineChoice('', _stored());
}

/** Choose the engine for the performance page's next load. A page already
 * playing keeps its engine: switching mid-set would cut the audio. */
export function setStoredEngineChoice(choice: EngineChoice): void {
	if (choice !== 'webaudio' && choice !== 'rust') {
		throw new Error(`audio_engine must be webaudio|rust, got ${String(choice)}`);
	}
	window.localStorage.setItem(ENGINE_PREF_KEY, choice);
}

let initialized = false;

/** Read the choice for this page. Runs once; later calls are no-ops. */
export function initRustMode(): void {
	if (initialized || typeof window === 'undefined') return;
	initialized = true;
	const stored = _stored();
	// Test stand-ins for window often carry no location.
	const search = window.location?.search ?? '';
	const params = new URLSearchParams(search);
	const choice = readEngineChoice(search, stored);
	if (params.has('engine')) {
		try {
			window.localStorage?.setItem(ENGINE_PREF_KEY, choice);
		} catch {
			// Not remembered; this page still uses the choice.
		}
	}
	const clock = params.get('engine_clock');
	if (clock !== null && clock !== 'wall' && clock !== 'device') {
		throw new Error(`engine_clock=${clock} is not one of wall, device`);
	}
	rustMode.clock = clock ?? 'device';
	rustMode.enabled = choice === 'rust';
}

let client: AudioEngineClient | null = null;
let connecting: Promise<AudioEngineClient> | null = null;
let rafId: number | null = null;
let lastState: EngineState | null = null;
/** Loop the anlz memory cue shows, to restore when an engine loop ends. */
const displayLoops: Partial<Record<DeckId, DeckState['loop']>> = {};
/**
 * Load fence per deck. The dispatcher names the new track on the deck before
 * the engine has swapped it, so a state frame from before the swap would paint
 * the old track's playhead onto the new one. While a load is in flight the
 * value is `Infinity` (mirror nothing); once it lands it is the last frame seen,
 * and only newer frames are mirrored.
 */
export const loadFences: Partial<Record<DeckId, number>> = {};

async function _json(path: string, init?: RequestInit): Promise<unknown> {
	const res = await fetch(`${API_BASE}${path}`, init);
	const body = await res.json().catch(() => null);
	if (!res.ok) {
		const b = (body ?? {}) as {
			error?: string;
			message?: string;
			detail?: unknown;
		};
		const why =
			b.message ?? (typeof b.detail === 'string' ? b.detail : JSON.stringify(b.detail ?? b));
		throw new Error(`${path} answered ${res.status}: ${b.error ?? ''} ${why}`.trim());
	}
	return body;
}

const _post = (path: string, body: unknown) =>
	_json(path, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body)
	});

async function _startEngine(): Promise<void> {
	let s = (await _json(AUDIO_ENGINE_PATH)) as AudioEngineStatus;
	if (s.state !== 'running' && s.state !== 'starting' && s.state !== 'restarting') {
		s = (await _post(`${AUDIO_ENGINE_PATH}/start`, {
			clock: rustMode.clock
		})) as AudioEngineStatus;
	}
	const deadline = performance.now() + 10_000;
	while (s.state !== 'running') {
		if (s.state === 'failed' || s.state === 'unavailable' || s.state === 'stopped') {
			throw new Error(`the Rust audio engine is ${s.state}: ${s.error ?? 'no reason given'}`);
		}
		if (performance.now() > deadline)
			throw new Error(`the Rust audio engine is still ${s.state} after 10 s`);
		await new Promise((r) => setTimeout(r, 100));
		s = (await _json(AUDIO_ENGINE_PATH)) as AudioEngineStatus;
	}
}

/** Connect (starting the engine if needed). Reused while the socket lives. */
export async function ensureRustEngine(): Promise<AudioEngineClient> {
	if (client?.connected) return client;
	if (connecting !== null) return connecting;
	rustMode.status = 'starting';
	connecting = (async () => {
		try {
			await _startEngine();
			const c = new AudioEngineClient({
				fetch: (url) => fetch(url),
				openSocket: (url) => new WebSocket(url) as never,
				now: () => performance.now(),
				apiBase: API_BASE,
				// A load waits on decode; everything else is one block away.
				commandTimeoutMs: 60_000
			});
			await c.connect();
			// A restarted engine counts frames from zero again.
			lastState = null;
			for (const k of Object.keys(loadFences) as unknown as DeckId[]) {
				if (loadFences[k] !== Infinity) loadFences[k] = -1;
			}
			c.onState(mirrorEngineState);
			// The engine outlives the page: after a reload it still holds the
			// previous page's decks. Silence what this page does not show.
			await Promise.all(
				([1, 2, 3, 4] as DeckId[])
					.filter((d) => deckStates[d].stable_id === null)
					.map((d) => c.send({ type: 'unload', deck: d }))
			);
			client = c;
			rustMode.status = 'connected';
			rustMode.error = null;
			_startRaf();
			return c;
		} catch (e) {
			rustMode.status = 'error';
			rustMode.error = e instanceof Error ? e.message : String(e);
			throw e;
		} finally {
			connecting = null;
		}
	})();
	return connecting;
}

/**
 * Run `command` on the Rust engine when this mode owns it.
 * Returns false when the command is the page's own (the caller runs it as usual).
 */
export async function executeInRustEngine(command: PerformanceCommand): Promise<boolean> {
	initRustMode();
	if (!rustMode.enabled) return false;
	const type = command.type;
	if (WEB_AUDIO_ONLY.has(type)) {
		throw new Error(
			`RUST_ENGINE_UNAVAILABLE: ${type} is not available with the Rust engine yet - see PARITY-TODO`
		);
	}
	if (!ENGINE_COMMANDS.has(type)) return false;
	const c = await ensureRustEngine();
	if (command.type === 'load') {
		await loadRustDeck(command.deck, command.stable_id);
		return true;
	}
	await c.send(command as unknown as { type: string } & Record<string, unknown>);
	applyAcknowledged(command);
	return true;
}

/** Load a library track: the page's metadata fetches plus the engine load. */
export async function loadRustDeck(deck: DeckId, stable_id: string): Promise<void> {
	if (stable_id.length === 0) throw new Error('load: stable_id must be non-empty');
	loadFences[deck] = Infinity;
	let trackRes, anlz, slots;
	try {
		[trackRes, anlz, slots] = await Promise.all([
			getTrack(stable_id),
			fetchAnlzForDeckLoad(stable_id),
			fetchHotCueSlots(stable_id),
			_post(`${AUDIO_ENGINE_PATH}/load`, { deck, stable_id })
		]);
	} finally {
		loadFences[deck] = lastState?.frame ?? -1;
	}
	const track = trackRes.track;
	const st = deckStates[deck];
	clearRustDeck(st);
	st.stable_id = stable_id;
	st.source_path =
		typeof track.file_path === 'string' && track.file_path.length > 0 ? track.file_path : null;
	st.title = track.title ?? null;
	st.artist = track.artist ?? null;
	st.rating = track.rating ?? null;
	st.key = track.key ?? null;
	// Until the engine's first state message says how long the decoded audio is.
	st.duration_ms = track.duration_ms ?? null;
	st.anlz = anlz;
	st.anlz_error = null;
	st.bpm = anlz.beatgrid.bpm ?? track.bpm ?? null;
	st.hot_cues = hotCuesFromAnlz(slots.flatMap((s) => (s.cue === null ? [] : [s.cue])));
	st.hot_cue_revisions = _hotCueRevisionsFrom(slots);
	st.has_rb_mapping = track.has_rb_mapping;
	st.loop = displayLoopFrom(anlz.cues, anlz.beatgrid.beats);
	displayLoops[deck] = st.loop;
	st.load_generation += 1;
	client?.send({ type: 'engine_state' }).catch(() => {});
}

/** Reset what a load publishes. Rust-mode twin of the Web Audio engine's
 * private `_clearLoadedTrackState`, minus its Web Audio runtime. */
export function clearRustDeck(st: DeckState): void {
	st.playing = false;
	st.audible = false;
	st.transport_pending = false;
	st.stable_id = null;
	st.source_path = null;
	st.title = null;
	st.artist = null;
	st.rating = null;
	st.bpm = null;
	st.key = null;
	st.duration_ms = null;
	st.position_ms = 0;
	st.cue_ms = null;
	st.pitch = 1;
	st.loop = null;
	st.hot_cues = [];
	st.anlz = null;
	st.anlz_error = null;
	st.processor_error = null;
	st.sync_error = null;
}

/** Knob positions follow what the engine accepted, not the state feed, so a
 * dragged control never jumps back to a 30 Hz-old value. */
export function applyAcknowledged(command: PerformanceCommand): void {
	switch (command.type) {
		case 'trim':
			mixerState.channels[command.deck].trim = command.value;
			break;
		case 'eq':
			mixerState.channels[command.deck][`eq_${command.band}`] = command.value;
			break;
		case 'filter':
			mixerState.channels[command.deck].filter = command.value;
			break;
		case 'fader':
			mixerState.channels[command.deck].fader = command.value;
			break;
		case 'assign':
			mixerState.channels[command.deck].assign = command.assign;
			break;
		case 'crossfader':
			mixerState.crossfader = command.value;
			break;
		case 'master_volume':
			mixerState.master = command.value;
			break;
		case 'pitch_range':
			pitchRanges[command.deck] = command.range;
			break;
		case 'unload':
			clearRustDeck(deckStates[command.deck]);
			break;
	}
}

/** Transport truth from the engine: play state, tempo, cue, loop, length. */
export function mirrorEngineState(s: EngineState): void {
	lastState = s;
	for (const d of s.decks) {
		const st = deckStates[d.deck as DeckId];
		if (st === undefined || st.stable_id === null) continue;
		if (s.frame <= (loadFences[d.deck as DeckId] ?? -1)) continue;
		st.playing = d.playing;
		st.audible = d.playing;
		st.transport_pending = false;
		st.pitch = d.tempo;
		if (d.loaded) st.duration_ms = d.duration_ms;
		st.cue_ms = d.cue_ms;
		st.loop = d.loop
			? {
					in_ms: d.loop.in_ms,
					out_ms: d.loop.out_ms,
					engaged: true,
					beat_length: null
				}
			: (displayLoops[d.deck as DeckId] ?? null);
		if (!d.playing) st.position_ms = d.position_ms;
	}
}

function _startRaf(): void {
	if (rafId !== null || typeof requestAnimationFrame === 'undefined') return;
	const tick = () => {
		rafId = requestAnimationFrame(tick);
		if (client === null || lastState === null) return;
		for (const d of lastState.decks) {
			const st = deckStates[d.deck as DeckId];
			if (st === undefined || st.stable_id === null || !d.playing) continue;
			if (lastState.frame <= (loadFences[d.deck as DeckId] ?? -1)) continue;
			const p = client.positionMs(d.deck);
			if (p !== null) st.position_ms = p;
		}
	};
	rafId = requestAnimationFrame(tick);
}
