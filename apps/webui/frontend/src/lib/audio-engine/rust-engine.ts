/**
 * Rust engine mode's connection to `odj-audio`: start and connect, load a
 * library track with its beatgrid, forward the commands the engine plays as
 * sent, and mirror the engine's state onto the page's stores. Loaded only
 * with the mode (see `rust-mode.svelte.ts`).
 */
import { API_BASE } from '$lib/api/base';
import { runAutomaticMasterElection } from '$lib/rb/master-election';
import { getTrack } from '$lib/api';
import { fetchAnlzForDeckLoad } from '$lib/components/rb/wave/anlz-cache.svelte';
import { _hotCueRevisionsFrom, deckStates, mixerState, pitchRanges } from '$lib/player/state.svelte';
import { fetchHotCueSlots } from '$lib/rb/api-rb';
import { displayLoopFrom } from '$lib/rb/beat-sync-math';
import type { DeckState } from '$lib/rb/deck-state-types';
import { hotCuesFromAnlz } from '$lib/rb/hot-cue-from-anlz';
import {
	installPerformanceHotCueDriver,
	type PerformanceCommand
} from '$lib/rb/performance-ipc.svelte';
import {
	AUDIO_ENGINE_PATH,
	AudioEngineClient,
	type AudioEngineStatus,
	type EngineState
} from './client';
import { DECKS, type DeckId, displayLoops, link, loadFences, send, type RustToast } from './rust-link';
import { PAGE_DECIDED, rustMode } from './rust-mode.svelte';
import { cancelArmedJump, decideOnPage, electIfAuto, rustHotCueDriver, rustMaster } from './rust-transport';

let rafId: number | null = null;
let connecting: Promise<AudioEngineClient> | null = null;

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
export async function connectRustEngine(): Promise<AudioEngineClient> {
	if (link.client?.connected) return link.client;
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
			link.lastState = null;
			for (const k of DECKS) {
				if (loadFences[k] !== Infinity) loadFences[k] = -1;
			}
			c.onState(mirrorEngineState);
			// The engine outlives the page: after a reload it still holds the
			// previous page's decks. Silence what this page does not show.
			await Promise.all(
				DECKS
					.filter((d) => deckStates[d].stable_id === null)
					.map((d) => c.send({ type: 'unload', deck: d }))
			);
			link.client = c;
			rustMode.notBuilt = [...c.notBuilt];
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
 * Run a command this mode owns (`ENGINE_COMMANDS`) on the engine; the eager
 * hook in `rust-mode.svelte.ts` has already refused the Web Audio only ones.
 */
export async function executeRustCommand(command: PerformanceCommand): Promise<void> {
	await connectRustEngine();
	if (command.type === 'load') {
		cancelArmedJump(command.deck);
		await loadRustDeck(command.deck, command.stable_id);
		return;
	}
	if (PAGE_DECIDED.has(command.type)) {
		await decideOnPage(command);
		return;
	}
	await send(command);
	applyAcknowledged(command);
	if (command.type === 'unload') {
		cancelArmedJump(command.deck);
		if (rustMaster.deck === command.deck) electIfAuto({ force: true });
	}
}

/** Stand in for the engine connection in unit tests. */
export function installRustEngineClientForTest(fake: AudioEngineClient | null): void {
	link.client = fake;
	rustMode.status = fake === null ? 'off' : 'connected';
	rustMode.notBuilt = fake === null ? [] : [...fake.notBuilt];
}

let active = false;

/** Take over the page's hot-cue seam. Runs once, when the mode's code loads. */
export function activateRustEngineMode(toast: RustToast | null): void {
	if (toast !== null) link.toast = toast;
	if (active) return;
	active = true;
	installPerformanceHotCueDriver(rustHotCueDriver);
}

/** Load a library track: the page's metadata fetches plus the engine load. */
export async function loadRustDeck(deck: DeckId, stable_id: string): Promise<void> {
	if (stable_id.length === 0) throw new Error('load: stable_id must be non-empty');
	deckStates[deck].load_generation += 1;
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
		loadFences[deck] = link.lastState?.frame ?? -1;
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
	link.client?.send({ type: 'engine_state' }).catch(() => {});
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

/** Transport truth from the engine: play state, tempo, loop, length, key. */
export function mirrorEngineState(s: EngineState): void {
	link.lastState = s;
	let masterStopped: { deck: DeckId; stableId: string; generation: number } | null = null;
	for (const d of s.decks) {
		const deck = d.deck as DeckId;
		const st = deckStates[deck];
		if (st === undefined || st.stable_id === null) continue;
		if (s.frame <= (loadFences[deck] ?? -1)) continue;
		// A track that plays out stops in the engine, not on a command.
		if (st.playing && !d.playing && rustMaster.deck === deck) {
			masterStopped = { deck, stableId: st.stable_id, generation: st.load_generation };
		}
		st.playing = d.playing;
		st.audible = d.playing;
		st.transport_pending = false;
		st.pitch = d.tempo;
		if (d.loaded) st.duration_ms = d.duration_ms;
		// The memory cue is the page's: pause and CUE set it, snapped to the
		// grid. The engine's own unsnapped cue is not mirrored.
		// Key lock and key shift as the engine plays them. A build without the
		// time-stretcher plays varispeed and unshifted, so MT shows off rather
		// than lit over audio that is not key-locked.
		if (d.master_tempo !== undefined) rustMode.keyLock = true;
		st.master_tempo_enabled = d.master_tempo ?? false;
		st.key_shift_semitones = d.key_shift_semitones ?? 0;
		st.loop = d.loop
			? {
					in_ms: d.loop.in_ms,
					out_ms: d.loop.out_ms,
					engaged: true,
					beat_length: d.loop.beat_length
				}
			: (displayLoops[d.deck as DeckId] ?? null);
		if (!d.playing) st.position_ms = d.position_ms;
	}
	if (masterStopped !== null) {
		const ended = masterStopped;
		void runAutomaticMasterElection(async () => {
			const st = deckStates[ended.deck];
			if (rustMaster.deck !== ended.deck || st.playing ||
				st.stable_id !== ended.stableId || st.load_generation !== ended.generation ||
				loadFences[ended.deck] === Infinity) return;
			electIfAuto();
		}).catch((error: unknown) => {
			rustMode.error = error instanceof Error ? error.message : String(error);
		});
	}
}

function _startRaf(): void {
	if (rafId !== null || typeof requestAnimationFrame === 'undefined') return;
	const tick = () => {
		rafId = requestAnimationFrame(tick);
		const { client, lastState } = link;
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
