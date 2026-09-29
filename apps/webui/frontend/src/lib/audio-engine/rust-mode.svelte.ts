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
 * - Beat sync, master election, quantize, CUE and hot-cue triggers are
 *   DECISIONS the page already makes with pure functions over our own
 *   beatgrids (`rust-sync.ts` names them). This mode makes the same decisions
 *   and sends the engine the result: a tempo ratio, a seek, a play.
 *
 * What the Rust engine does not do yet is refused, never faked: its own
 * commands it has not built (keylock, stems, the headphone cue bus) come back
 * `not_implemented` naming the plan that brings them, the engine lists them in
 * its hello so the page grays those controls out (`rustCommandUnsupported`),
 * and page features that are bound to Web Audio internals throw
 * `RUST_ENGINE_UNAVAILABLE` naming the command. Everything else (library,
 * panels, feedback, hot-cue editing, preview) runs on the page as before.
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
import { effectiveBeatSync, gridFeatureInertTip, gridFeaturesInert } from '$lib/player/grid-features';
import { fetchHotCueSlots } from '$lib/rb/api-rb';
import {
	assertPausedMasterSelectionAllowed,
	masterSwitchFollowers,
	pausedMasterSelectionBlockers
} from '$lib/rb/audio-engine-guards';
import { syncModeForBeatSyncMax } from '$lib/rb/beat-sync-decisions';
import { displayLoopFrom } from '$lib/rb/beat-sync-math';
import type { DeckId } from '$lib/rb/deck-slots';
import type { DeckState } from '$lib/rb/deck-state-types';
import { hotCuesFromAnlz } from '$lib/rb/hot-cue-from-anlz';
import { electMaster } from '$lib/rb/master-election';
import {
	installPerformanceHotCueDriver,
	type PerformanceCommand,
	type PerformanceHotCueDriver
} from '$lib/rb/performance-ipc.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { pushToast } from '$lib/stores.svelte';
import {
	AUDIO_ENGINE_PATH,
	AudioEngineClient,
	type AudioEngineStatus,
	type EngineCommand,
	type EngineState
} from './client';
import {
	electionInputFrom,
	lateJumpPositionMs,
	planRustFollowerJoin,
	reanchorNeedsSeek,
	rustCuePoint,
	rustSeekTarget,
	SYNC_LEAD_SEC,
	type SyncDeckView
} from './rust-sync';

export const ENGINE_PREF_KEY = 'odj.audioEngine';

/** Commands the engine plays as the page sends them. The ones this build has
 * not made yet come back `not_implemented` naming the plan that brings them,
 * and the engine lists them in its hello (`rustMode.notBuilt`). */
export const FORWARDED: ReadonlySet<string> = new Set([
	'unload',
	'loop',
	'beat_loop',
	'beat_jump',
	'pitch_range',
	'master_tempo',
	'key_nudge',
	'trim',
	'eq',
	'filter',
	'fader',
	'assign',
	'crossfader',
	'master_volume',
	'master_mute',
	// Engine commands for later plans (protocol.rs LATER).
	'key_sync',
	'stem_mute',
	'stem_solo',
	'stem_gain',
	'stem_eq_mode',
	'slip',
	'channel_cue',
	'headphone_mix',
	'headphone_level',
	'head_delay_ms',
	'output_mode',
	'headphone_output_select',
	'headphone_master_select'
]);

/** Commands whose decisions the page makes on its own beatgrids (the same pure
 * functions the Web Audio engine uses), sending the engine the outcome. */
export const PAGE_DECIDED: ReadonlySet<string> = new Set([
	'play',
	'cue',
	'seek',
	'tempo',
	'beat_sync',
	'sync_mode',
	'master',
	'quantize',
	'quantize_grid'
]);

/** Every command this mode takes from the page. Hot-cue save, clear, restore
 * and trigger stay the page's own and reach the engine through
 * `rustHotCueDriver`; preview plays on the page's own preview player. */
export const ENGINE_COMMANDS: ReadonlySet<string> = new Set(['load', ...FORWARDED, ...PAGE_DECIDED]);

/** Page features whose implementation reaches into the Web Audio engine's
 * own clock, buffers or devices. Refused by name in this mode. */
export const WEB_AUDIO_ONLY: ReadonlySet<string> = new Set([
	'load_play_intent',
	'loop_interval_mode',
	'loop_interval_base',
	'safety_loop_save',
	'safety_loop_arm',
	'safety_loop_clear',
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
	error: null as string | null,
	/** What the connected engine build refuses (its hello). */
	notBuilt: [] as string[],
	/** Whether the engine reports key lock state (a build with the stretcher). */
	keyLock: false
});

/**
 * Whether a control that sends `type` does nothing in this mode, so the UI
 * renders it inert with the PARITY-TODO title instead of failing on press.
 * False whenever the Rust engine mode is off.
 */
export function rustCommandUnsupported(type: string): boolean {
	initRustMode();
	if (!rustMode.enabled) return false;
	if (WEB_AUDIO_ONLY.has(type)) return true;
	if (type === 'master_tempo') return !rustMode.keyLock;
	return FORWARDED.has(type) && rustMode.notBuilt.includes(type);
}

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
	if (rustMode.enabled) installPerformanceHotCueDriver(rustHotCueDriver);
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
	await ensureRustEngine();
	if (command.type === 'load') {
		_cancelArmedJump(command.deck);
		await loadRustDeck(command.deck, command.stable_id);
		return true;
	}
	if (PAGE_DECIDED.has(type)) {
		await _decide(command);
		return true;
	}
	await _send(command as unknown as EngineCommand);
	applyAcknowledged(command);
	if (command.type === 'unload') {
		_cancelArmedJump(command.deck);
		if (rustMaster.deck === command.deck) _electIfAuto({ force: true });
	}
	return true;
}

function _send(cmd: EngineCommand): Promise<unknown> {
	if (client === null) throw new Error('the Rust audio engine is not connected');
	return client.send(cmd);
}

/** Stand in for the engine connection in unit tests. */
export function installRustEngineClientForTest(fake: AudioEngineClient | null): void {
	client = fake;
	rustMode.status = fake === null ? 'off' : 'connected';
	rustMode.notBuilt = fake === null ? [] : [...fake.notBuilt];
}

// ------------------------------------------------------ page-decided commands

/** The page's master, as the Web Audio engine keeps `_masterDeck` and
 * `_masterMode`. `is_master` on each deck mirrors `deck`. */
export const rustMaster: { deck: DeckId | null; mode: 'auto' | 'locked' } = {
	deck: null,
	mode: 'auto'
};

const DECKS = [1, 2, 3, 4] as DeckId[];

function _assignMaster(deck: DeckId | null): void {
	rustMaster.deck = deck;
	for (const d of DECKS) deckStates[d].is_master = d === deck;
}

/** The master that can drive Beat Sync phase: set and playing. */
function _syncMaster(): DeckId | null {
	const d = rustMaster.deck;
	return d !== null && deckStates[d].playing ? d : null;
}

function _electIfAuto(options?: { force?: boolean }): void {
	if (rustMaster.mode === 'locked' && !options?.force) return;
	if (options?.force) rustMaster.mode = 'auto';
	_assignMaster(electMaster(electionInputFrom(deckStates, mixerState)));
}

function _positionMs(deck: DeckId): number {
	const st = deckStates[deck];
	if (!st.playing) return st.position_ms;
	return client?.positionMs(deck) ?? st.position_ms;
}

function _view(deck: DeckId, positionSec?: number): SyncDeckView {
	const st = deckStates[deck];
	return {
		beats: st.anlz?.beatgrid.beats ?? [],
		playing: st.playing,
		positionSec: positionSec ?? _positionMs(deck) / 1000,
		tempo: st.pitch
	};
}

function _loaded(deck: DeckId, what: string): DeckState {
	const st = deckStates[deck];
	if (st.stable_id === null) throw new Error(`${what}: deck ${deck} is not loaded`);
	return st;
}

function _pitchBounds(deck: DeckId): { min: number; max: number } {
	const range = pitchRanges[deck] / 100;
	return { min: Math.max(0.01, 1 - range), max: 1 + range };
}

function _setPlaying(deck: DeckId, playing: boolean): void {
	const st = deckStates[deck];
	st.playing = playing;
	st.audible = playing;
	st.transport_pending = false;
}

/**
 * Lock `follower` to `master`. `play` starts it on the lock; `reanchor`
 * re-phases a follower that is already locked, seeking only when its phase
 * is off (`reanchorNeedsSeek`). A plan that cannot phase-lock throws.
 */
async function _join(
	master: DeckId,
	follower: DeckId,
	options: {
		play?: boolean;
		reanchor?: boolean;
		masterAtSec?: number | undefined;
		followerAtSec?: number;
	} = {}
): Promise<void> {
	const st = deckStates[follower];
	const fv = _view(follower);
	const join = planRustFollowerJoin(_view(master, options.masterAtSec), fv, {
		leadSec: SYNC_LEAD_SEC,
		mode: syncModeForBeatSyncMax(uiPrefs.beat_sync_max, st.sync_mode),
		pitchRangePct: pitchRanges[follower],
		...(options.followerAtSec === undefined ? {} : { followerAtSec: options.followerAtSec })
	});
	const cmds: EngineCommand[] = [{ type: 'tempo', deck: follower, ratio: join.tempo }];
	if (!options.reanchor || reanchorNeedsSeek(join, fv, SYNC_LEAD_SEC)) {
		cmds.push({ type: 'seek', deck: follower, position_ms: join.positionMs });
	}
	if (options.play) cmds.push({ type: 'play', deck: follower, playing: true });
	// Sent together: the engine applies what arrives before its next block
	// in that block, so tempo, position and start land as one.
	await Promise.all(cmds.map(_send));
	st.pitch = join.tempo;
	st.sync_error = null;
	if (options.play) _setPlaying(follower, true);
}

/** Re-phase playing followers after their master moved. A follower that
 * cannot lock keeps playing and says why in `sync_error`, as on Web Audio. */
async function _reanchor(
	master: DeckId,
	followers: readonly DeckId[],
	masterAtSec?: number
): Promise<void> {
	await Promise.all(
		followers.map((f) =>
			_join(master, f, { reanchor: true, masterAtSec }).catch((e: unknown) => {
				deckStates[f].sync_error = e instanceof Error ? e.message : String(e);
			})
		)
	);
}

function _lockedFollowers(master: DeckId): DeckId[] {
	return DECKS.filter(
		(d) => d !== master && deckStates[d].playing && effectiveBeatSync(deckStates[d])
	);
}

async function _play(deck: DeckId): Promise<void> {
	const st = _loaded(deck, 'play');
	const syncClock = _syncMaster();
	const syncActive = effectiveBeatSync(st);
	if (syncClock !== null && syncClock !== deck && syncActive) {
		try {
			await _join(syncClock, deck, { play: true });
		} catch (e) {
			const detail = e instanceof Error ? e.message : String(e);
			st.sync_error = detail;
			throw new Error(
				`Beat Sync: deck ${deck} could not phase-lock to deck ${syncClock} (${detail})`,
				{ cause: e }
			);
		}
		return;
	}
	await _send({ type: 'play', deck, playing: true });
	_setPlaying(deck, true);
	st.sync_error = null;
	if (rustMaster.mode === 'auto' && syncClock === null) {
		_electIfAuto();
		const elected = _syncMaster();
		if (elected !== null && elected !== deck && syncActive) {
			await _join(elected, deck, { reanchor: true });
		}
	}
}

async function _pause(deck: DeckId): Promise<void> {
	const st = _loaded(deck, 'pause');
	const at = _positionMs(deck);
	_cancelArmedJump(deck);
	await _send({ type: 'play', deck, playing: false });
	_setPlaying(deck, false);
	st.position_ms = at;
	// The memory cue a pause leaves, snapped as the Web Audio engine snaps it.
	st.cue_ms = rustCuePoint(st, at);
	if (rustMaster.deck === deck) _electIfAuto();
}

/** The CUE button. Playing: back to the cue point and pause. Paused with no
 * cue: set it here. Paused with a cue: go to it. */
async function _pressCue(deck: DeckId): Promise<void> {
	const st = _loaded(deck, 'pressCue');
	if (st.playing) {
		const target = st.cue_ms ?? 0;
		_cancelArmedJump(deck);
		await Promise.all([
			_send({ type: 'seek', deck, position_ms: target }),
			_send({ type: 'play', deck, playing: false })
		]);
		_setPlaying(deck, false);
		st.position_ms = target;
		if (rustMaster.deck === deck) _electIfAuto();
		return;
	}
	if (st.cue_ms === null) {
		st.cue_ms = rustCuePoint(st, st.position_ms);
		return;
	}
	await _seek(deck, st.cue_ms, { quantize: true });
}

/** A seek: snapped to the deck's quantize grid when asked, out of an engaged
 * loop when it lands outside it, and re-phased when the deck is a follower
 * (or, with Beat Sync Max, when it is the master). */
async function _seek(deck: DeckId, ms: number, options: { quantize: boolean }): Promise<void> {
	const st = _loaded(deck, 'seek');
	const durMs = st.duration_ms ?? Infinity;
	if (!Number.isFinite(ms) || ms < 0 || ms > durMs) {
		throw new RangeError(`seek: ms must be within 0..${Math.round(durMs)}, got ${ms}`);
	}
	const { targetMs, exitLoop } = rustSeekTarget(
		options.quantize ? st : { ...st, quantize_enabled: false },
		ms
	);
	if (targetMs > durMs) {
		throw new RangeError(`seek: quantized target ${targetMs} exceeds duration ${durMs}`);
	}
	_cancelArmedJump(deck);
	if (exitLoop) {
		// Rekordbox: a seek outside an engaged loop leaves it.
		await _send({ type: 'loop', deck, loop: null });
		displayLoops[deck] = null;
		st.loop = null;
	}
	const master = _syncMaster();
	if (st.playing && effectiveBeatSync(st) && master !== null && master !== deck) {
		await _join(master, deck, { followerAtSec: targetMs / 1000 });
		st.position_ms = targetMs;
		return;
	}
	await _send({ type: 'seek', deck, position_ms: targetMs });
	st.position_ms = targetMs;
	if (st.playing && uiPrefs.beat_sync_max && master === deck) {
		await _reanchor(deck, _lockedFollowers(deck), targetMs / 1000);
	}
}

async function _setTempo(deck: DeckId, ratio: number): Promise<void> {
	const st = _loaded(deck, 'setTempoRatio');
	if (!Number.isFinite(ratio) || ratio <= 0) {
		throw new RangeError(`setTempoRatio: ratio must be > 0, got ${ratio}`);
	}
	const rangePct = pitchRanges[deck];
	if (Math.abs(ratio - 1) * 100 > rangePct + 1e-9) {
		throw new RangeError(
			`setTempoRatio: ratio ${ratio} outside the selected +-${rangePct}% range on deck ${deck}`
		);
	}
	const master = _syncMaster();
	if (st.playing && effectiveBeatSync(st) && master !== null && master !== deck) {
		throw new Error(`setTempoRatio: disable Beat Sync before changing follower deck ${deck}`);
	}
	await _send({ type: 'tempo', deck, ratio });
	st.pitch = ratio;
	if (master === deck) await _reanchor(deck, _lockedFollowers(deck));
}

async function _setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {
	if (typeof enabled !== 'boolean') throw new TypeError('setBeatSync: enabled must be boolean');
	const st = deckStates[deck];
	st.beat_sync_enabled = enabled;
	if (!enabled) {
		st.sync_error = null;
		return;
	}
	// Same contract as Web Audio: the flag keeps the DJ's choice, a gridless
	// deck has nothing to lock to, and that is said out loud.
	if (gridFeaturesInert(st)) {
		pushToast(`Deck ${deck} BEAT SYNC ${gridFeatureInertTip(st)}`, 'info');
		return;
	}
	if (!st.playing) return;
	let master = _syncMaster();
	if (master === null) {
		if (rustMaster.mode === 'locked' && rustMaster.deck !== null) return;
		_electIfAuto();
		master = _syncMaster();
	}
	if (master === null || master === deck) return;
	try {
		await _join(master, deck);
	} catch (e) {
		st.beat_sync_enabled = false;
		const { min, max } = _pitchBounds(deck);
		const detail = e instanceof Error ? e.message : String(e);
		throw new Error(`cannot phase-lock within pitch [${min}, ${max}] (BAR): ${detail}`, {
			cause: e
		});
	}
}

async function _setDeckMaster(deck: DeckId, lock: boolean | undefined): Promise<void> {
	const st = _loaded(deck, 'setDeckMaster');
	const applyLock = (): void => {
		if (lock === true) rustMaster.mode = 'locked';
		else if (lock === false) {
			rustMaster.mode = 'auto';
			if (!st.playing) _electIfAuto();
		}
	};
	if (!st.playing) {
		if (lock === false && rustMaster.deck === deck) return applyLock();
		assertPausedMasterSelectionAllowed(
			deck,
			st.audible,
			pausedMasterSelectionBlockers(deck, deckStates)
		);
		_assignMaster(deck);
		return applyLock();
	}
	const followers = masterSwitchFollowers(deck, deckStates).filter((d) =>
		effectiveBeatSync(deckStates[d])
	);
	_assignMaster(deck);
	await _reanchor(deck, followers);
	applyLock();
}

async function _decide(command: PerformanceCommand): Promise<void> {
	switch (command.type) {
		case 'play':
			// Quantized and scheduled launches go to the engine, which refuses
			// them by name rather than starting off the grid.
			if (command.quantize === true || command.start_at_context_sec !== undefined) {
				await _send(command as unknown as EngineCommand);
				return;
			}
			return command.playing ? _play(command.deck) : _pause(command.deck);
		case 'cue':
			return _pressCue(command.deck);
		case 'seek':
			return _seek(command.deck, command.position_ms, { quantize: true });
		case 'tempo':
			return _setTempo(command.deck, command.ratio);
		case 'beat_sync':
			return _setBeatSync(command.deck, command.enabled);
		case 'sync_mode': {
			if (command.mode !== 'beat' && command.mode !== 'bar') {
				throw new TypeError(`setSyncMode: invalid sync mode ${String(command.mode)}`);
			}
			const st = deckStates[command.deck];
			st.sync_mode = command.mode;
			const master = _syncMaster();
			if (st.playing && effectiveBeatSync(st) && master !== null && master !== command.deck) {
				await _join(master, command.deck, { reanchor: true });
			}
			return;
		}
		case 'master':
			return _setDeckMaster(command.deck, command.lock);
		case 'quantize': {
			if (typeof command.enabled !== 'boolean') {
				throw new TypeError('setQuantize: enabled must be boolean');
			}
			const st = deckStates[command.deck];
			st.quantize_enabled = command.enabled;
			if (command.enabled && gridFeaturesInert(st)) {
				pushToast(`Deck ${command.deck} QUANTIZE ${gridFeatureInertTip(st)}`, 'info');
			}
			return;
		}
		case 'quantize_grid':
			if (command.beats !== 1 && command.beats !== 4 && command.beats !== 8) {
				throw new TypeError('setQuantizeGrid: beats must be 1, 4, or 8');
			}
			deckStates[command.deck].quantize_grid_beats = command.beats;
			return;
		default:
			throw new Error(`${command.type} is not decided by the page in Rust engine mode`);
	}
}

// ------------------------------------------------------------------ hot cues

/** Armed jumps waiting on their downbeat, per deck. */
const armedJumps: Partial<Record<DeckId, ReturnType<typeof setTimeout>>> = {};

function _cancelArmedJump(deck: DeckId): void {
	const t = armedJumps[deck];
	if (t !== undefined) clearTimeout(t);
	delete armedJumps[deck];
}

const _nowSec = (): number => performance.now() / 1000;

/**
 * The page's hot-cue logic (`hot_cue_*` in performance-ipc) driving this
 * engine: the same slots, trust gate and `planHotCueTrigger` decision. An
 * immediate trigger is a quantized seek. An armed one waits for the planned
 * downbeat on the page clock, then seeks as far past the cue as the music
 * already went, so it lands on the beat even when the timer fires late.
 */
export const rustHotCueDriver: PerformanceHotCueDriver = {
	stableId: (deck) => deckStates[deck].stable_id,
	refresh: async (deck) => {
		const st = _loaded(deck, 'hot cue refresh');
		const id = st.stable_id as string;
		const slots = await fetchHotCueSlots(id);
		if (st.stable_id !== id) return;
		st.hot_cues = hotCuesFromAnlz(slots.flatMap((s) => (s.cue === null ? [] : [s.cue])));
		st.hot_cue_revisions = _hotCueRevisionsFrom(slots);
	},
	hasRbMapping: (deck) => deckStates[deck].has_rb_mapping,
	triggerState: (deck, slot) => {
		const st = deckStates[deck];
		return {
			cue: st.hot_cues.find((c) => c.slot === slot) ?? null,
			playing: st.playing,
			loopEngaged: st.loop !== null && st.loop.engaged,
			positionSec: _positionMs(deck) / 1000,
			beats: st.anlz?.beatgrid.beats ?? []
		};
	},
	jump: (deck, positionMs) => _seek(deck, positionMs, { quantize: true }),
	arm: async (deck, positionMs, armAtPositionSec) => {
		const st = _loaded(deck, 'armHotCueTrigger');
		const nowPositionSec = _positionMs(deck) / 1000;
		if (armAtPositionSec < nowPositionSec) {
			throw new RangeError(
				`armHotCueTrigger: armAtPositionSec ${armAtPositionSec} precedes current position ${nowPositionSec}`
			);
		}
		_cancelArmedJump(deck);
		const delaySec = (armAtPositionSec - nowPositionSec) / st.pitch;
		const landsAt = _nowSec() + delaySec;
		const generation = st.load_generation;
		armedJumps[deck] = setTimeout(() => {
			delete armedJumps[deck];
			if (st.load_generation !== generation || !st.playing) return;
			const target = lateJumpPositionMs(positionMs, _nowSec() - landsAt, st.pitch);
			_seek(deck, target, { quantize: false }).catch((e: unknown) => {
				pushToast(`Deck ${deck} hot cue: ${e instanceof Error ? e.message : String(e)}`, 'error');
			});
		}, delaySec * 1000);
		return landsAt;
	},
	contextTimeNowSec: _nowSec
};

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

/** Transport truth from the engine: play state, tempo, loop, length, key. */
export function mirrorEngineState(s: EngineState): void {
	lastState = s;
	let masterStopped = false;
	for (const d of s.decks) {
		const deck = d.deck as DeckId;
		const st = deckStates[deck];
		if (st === undefined || st.stable_id === null) continue;
		if (s.frame <= (loadFences[deck] ?? -1)) continue;
		// A track that plays out stops in the engine, not on a command.
		if (st.playing && !d.playing && rustMaster.deck === deck) masterStopped = true;
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
					beat_length: null
				}
			: (displayLoops[d.deck as DeckId] ?? null);
		if (!d.playing) st.position_ms = d.position_ms;
	}
	if (masterStopped) _electIfAuto();
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
