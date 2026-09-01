/**
 * Typed, fail-fast browser command surface for /performance.
 *
 * UI controls and browser automation both dispatch through the same command
 * function. Runtime validation rejects malformed IPC messages, while command
 * failures are recorded in reactive state and shown by each deck.
 *
 * Requirements:
 *   ✔︎ Independent deck queues prevent one slow load from blocking another.
 *     [if] deck 1 load is pending [then] deck 2 load starts independently
 *   ✔︎ Sync-sensitive transport uses one explicit coordination scope.
 *     [if] master/follower commands overlap [then] their engine writes serialize
 *   ✔︎ Continuous mixer controls execute immediately and round-trip in query().
 *     [if] an agent moves EQ [then] the visible UI and IPC snapshot both update
 *   ✔︎ Preset phases form one barrier across every deck and sync coordination.
 *     [if] a preset is claimed while deck work is active [then] it waits for
 *       every prior scope and no later command can overlap it
 *   ✔︎ ✅ 🎯 Route command sessions invalidate queued work before audio teardown.
 *     [if] load/play commands are queued when IPC uninstalls [then] they reject
 *       before invoking the engine or recreating an off-route audio graph ⛔️
 *     [if] a new route session starts while old work settles [then] its commands
 *       use fresh scheduler tails and clean pending counters
 */

import { pushToast } from '$lib/stores.svelte';
import { clearHotCue, restoreHotCue, saveHotCue } from '$lib/rb/api-rb';
import { bootScheduler } from '$lib/rb/boot-scheduler';
import {
	DECK_IDS,
	deckEffectiveBpm,
	deckTransportClock,
	engine,
	getDeckState,
	mixerState,
	pitchRanges,
	type DeckTransportClock,
	type PitchRange
} from '$lib/rb/audio-engine.svelte';
import {
	ScopedCommandInvalidatedError,
	ScopedCommandScheduler
} from '$lib/rb/performance-command-scheduler';
import type {
	CrossfaderAssign,
	DeckAudioSnapshot,
	DeckId,
	DeckState,
	EqBand,
	HeadphoneState,
	HotCue,
	HotCueSlot,
	LoopState,
	MixerChannelState,
	StemControl,
	StemDeckState,
	SyncMode
} from '$lib/rb/types';
import type { PerformancePresetPhase } from '$lib/rb/performance-preset';
import { noteRecentDeck } from '$lib/rb/recent-deck';

export type PerformanceCommand =
	| { type: 'load'; deck: DeckId; stable_id: string }
	| { type: 'unload'; deck: DeckId }
	| { type: 'play'; deck: DeckId; playing: boolean }
	| { type: 'cue'; deck: DeckId }
	| { type: 'seek'; deck: DeckId; position_ms: number }
	| { type: 'loop'; deck: DeckId; loop: { in_ms: number; out_ms: number } | null }
	| { type: 'beat_loop'; deck: DeckId; beats: number; start_ms?: number }
	| { type: 'tempo'; deck: DeckId; ratio: number }
	| { type: 'pitch_range'; deck: DeckId; range: PitchRange }
	| { type: 'quantize'; deck: DeckId; enabled: boolean }
	| { type: 'beat_sync'; deck: DeckId; enabled: boolean }
	| { type: 'sync_mode'; deck: DeckId; mode: SyncMode }
	| { type: 'master'; deck: DeckId }
	| { type: 'master_tempo'; deck: DeckId; enabled: boolean }
	| { type: 'stem_mute'; deck: DeckId; stem: StemControl; muted: boolean }
	| { type: 'stem_solo'; deck: DeckId; stem: StemControl; solo: boolean }
	| { type: 'slip'; deck: DeckId; enabled: boolean }
	| { type: 'key_sync'; deck: DeckId; enabled: boolean }
	| { type: 'key_nudge'; deck: DeckId; semitones: -1 | 1 }
	| { type: 'trim'; deck: DeckId; value: number }
	| { type: 'eq'; deck: DeckId; band: EqBand; value: number }
	| { type: 'fader'; deck: DeckId; value: number }
	| { type: 'assign'; deck: DeckId; assign: CrossfaderAssign }
	| { type: 'channel_cue'; deck: DeckId; enabled: boolean }
	| { type: 'crossfader'; value: number }
	| { type: 'master_volume'; value: number }
	| { type: 'headphone_mix'; value: number }
	| { type: 'headphone_level'; value: number }
	| { type: 'headphone_outputs_refresh' }
	| { type: 'headphone_output_acquire' }
	| { type: 'headphone_output_select'; device_id: string }
	| { type: 'safety_loop_save'; deck: DeckId }
	| { type: 'safety_loop_arm'; deck: DeckId; armed: boolean }
	| { type: 'safety_loop_clear'; deck: DeckId }
	| { type: 'hot_cue_save'; deck: DeckId; slot: HotCueSlot; in_ms: number; revision: string }
	| { type: 'hot_cue_clear'; deck: DeckId; slot: HotCueSlot; revision: string }
	| { type: 'hot_cue_restore'; deck: DeckId; slot: HotCueSlot; revision: string; reversal_id: string };

export interface PerformanceDeckSnapshot {
	deck_id: DeckId;
	stable_id: string | null;
	title: string | null;
	artist: string | null;
	bpm: number | null;
	key: string | null;
	key_shift_semitones: number;
	effective_bpm: number | null;
	duration_ms: number | null;
	position_ms: number;
	playing: boolean;
	audible: boolean;
	transport_pending: boolean;
	transport_clock: DeckTransportClock;
	cue_ms: number | null;
	pitch: number;
	pitch_range: PitchRange;
	quantize_enabled: boolean;
	beat_sync_enabled: boolean;
	key_sync_enabled: boolean;
	master_tempo_enabled: boolean;
	slip_enabled: boolean;
	slip_active: boolean;
	slip_position_ms: number | null;
	is_master: boolean;
	sync_mode: SyncMode;
	sync_error: string | null;
	processor_error: string | null;
	/** Last successful load wall ms; null until a load completes (KPI). */
	last_load_latency_ms: number | null;
	/** Last load stage map (ms); null until a load. CLI/IPC feedback. */
	last_load_stages: Record<string, number> | null;
	stems: StemDeckState;
	loop: LoopState | null;
	beatgrid: Array<{ n: number; bpm: number; time_ms: number }>;
	beatgrid_ms: number[];
	hot_cue_slots: Array<{ slot: HotCueSlot; cue: HotCue | null; revision: string }>;
	hot_cue_reversal: { slot: HotCueSlot; revision: string; reversal_id: string } | null;
	command_error: string | null;
	command_pending: boolean;
}

export interface PerformanceState {
	version: 1;
	master_deck: DeckId | null;
	command_pending: boolean;
	command_queued: number;
	decks: Record<DeckId, PerformanceDeckSnapshot>;
	mixer: {
		crossfader: number;
		master: number;
		channels: Record<DeckId, MixerChannelState>;
		headphones: HeadphoneState;
	};
	preset: PerformancePresetLifecycleSnapshot;
	last_error: string | null;
}

export type PerformancePresetLifecyclePhase =
	| 'idle'
	| 'queued'
	| PerformancePresetPhase
	| 'ready'
	| 'error';

export interface PerformancePresetLifecycleSnapshot {
	id: string | null;
	phase: PerformancePresetLifecyclePhase;
	active: boolean;
	error: string | null;
}

export interface PerformancePresetTransactionDriver {
	dispatch(command: PerformanceCommand): Promise<PerformanceState>;
	query(): PerformanceState;
	setPhase(phase: PerformancePresetPhase): void;
}

export interface PerformanceBrowserIpc {
	readonly version: 1;
	/**
	 * Q1 agent-native parity: `pressT0Ms` is the same optional input stamp the
	 * UI boundary takes, on the `performance.now()` epoch. A browser agent
	 * driving a transport command passes its own stamp and gets the same
	 * `press_to_schedule_ms` / `input_to_audible_ms` stages a human press
	 * produces; omitting it (autoplay, preset restore, any command with no
	 * input behind it) leaves those stages off the row rather than reporting a
	 * press that never happened.
	 */
	dispatch(message: unknown, pressT0Ms?: number): Promise<PerformanceState>;
	query(): PerformanceState;
	capture(deck: unknown): DeckAudioSnapshot;
	/** Agent-native twin of the deck error banner's dismiss control. */
	dismissDeckError(deck: unknown): PerformanceState;
}

export const performanceCommandStatus: {
	last_error: string | null;
	deck_errors: Record<DeckId, string | null>;
	deck_pending: Record<DeckId, number>;
	active: number;
	queued: number;
} = $state({
	last_error: null,
	deck_errors: { 1: null, 2: null, 3: null, 4: null },
	deck_pending: { 1: 0, 2: 0, 3: 0, 4: 0 },
	active: 0,
	queued: 0
});

export const performancePresetLifecycle: PerformancePresetLifecycleSnapshot = $state({
	id: null,
	phase: 'idle',
	active: false,
	error: null
});

const hotCueReversals: Record<DeckId, { slot: HotCueSlot; revision: string; reversal_id: string } | null> =
	$state({ 1: null, 2: null, 3: null, 4: null });

export interface PerformanceHotCueDriver {
	stableId(deck: DeckId): string | null;
	refresh(deck: DeckId): Promise<void>;
}

const _defaultHotCueDriver: PerformanceHotCueDriver = {
	stableId: (deck) => getDeckState(deck).stable_id,
	refresh: (deck) => engine.refreshHotCues(deck)
};
let _hotCueDriver: PerformanceHotCueDriver = _defaultHotCueDriver;

/** Narrow test seam for exercising the public IPC command protocol without
 * initializing Web Audio. Production always uses the engine-owned driver. */
export function installPerformanceHotCueDriverForTest(driver: PerformanceHotCueDriver): () => void {
	const previous = _hotCueDriver;
	_hotCueDriver = driver;
	return () => {
		_hotCueDriver = previous;
	};
}

let _presetClaim: { id: string } | null = null;
type CommandScope = DeckId | 'sync' | 'headphone';
const _commandScheduler = new ScopedCommandScheduler<CommandScope>();
let _commandGeneration = 0;
let _commandStatusGeneration = 0;
let _activeCommandSession: { generation: number } | null = null;
export const PERFORMANCE_PRESET_COMMAND_SCOPES: readonly CommandScope[] = [
	...DECK_IDS,
	'sync',
	'headphone'
];

declare global {
	interface Window {
		musicDjToolsPerformance?: PerformanceBrowserIpc;
	}
}

type UnknownRecord = Record<string, unknown>;

// ---------------------------------------------------------------- validation

function _record(message: unknown): UnknownRecord {
	if (typeof message !== 'object' || message === null || Array.isArray(message)) {
		throw new TypeError('performance command must be a plain object');
	}
	return message as UnknownRecord;
}

function _exactKeys(record: UnknownRecord, keys: readonly string[]): void {
	const allowed = new Set(keys);
	const unexpected = Object.keys(record).filter((key) => !allowed.has(key));
	if (unexpected.length > 0) {
		throw new TypeError(`performance command has unexpected fields: ${unexpected.join(', ')}`);
	}
}

function _deck(value: unknown): DeckId {
	if (value !== 1 && value !== 2 && value !== 3 && value !== 4) {
		throw new RangeError(`deck must be one of 1, 2, 3, 4; got ${String(value)}`);
	}
	return value;
}

function _boolean(name: string, value: unknown): boolean {
	if (typeof value !== 'boolean') throw new TypeError(`${name} must be boolean`);
	return value;
}

function _finite(name: string, value: unknown): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new TypeError(`${name} must be a finite number`);
	}
	return value;
}

function _unit(name: string, value: unknown): number {
	const parsed = _finite(name, value);
	if (parsed < 0 || parsed > 1) throw new RangeError(`${name} must be within 0..1`);
	return parsed;
}

function _stem(value: unknown): StemControl {
	if (value !== 'vocal' && value !== 'instrumental' && value !== 'drums') {
		throw new TypeError(`stem must be vocal, instrumental, or drums; got ${String(value)}`);
	}
	return value;
}

function _hotCueSlot(value: unknown): HotCueSlot {
	if (value !== 'A' && value !== 'B' && value !== 'C' && value !== 'D' &&
		value !== 'E' && value !== 'F' && value !== 'G' && value !== 'H') {
		throw new TypeError(`hot-cue slot must be A through H; got ${String(value)}`);
	}
	return value;
}

function _revision(name: string, value: unknown): string {
	if (typeof value !== 'string' || value.length === 0) {
		throw new TypeError(`${name} must be a non-empty revision`);
	}
	return value;
}

/**
 * Q1: the press stamp crosses the browser IPC boundary like any other input,
 * so it gets the same fail-fast validation every command field gets. A string
 * or a NaN here would silently disable the two press stages on a row that
 * still looks complete, which is worse than a rejected command.
 */
function _validatedPressStamp(pressT0Ms: unknown): number | undefined {
	if (pressT0Ms === undefined) return undefined;
	if (typeof pressT0Ms !== 'number' || !Number.isFinite(pressT0Ms) || pressT0Ms < 0) {
		throw new TypeError(
			`press stamp must be a finite non-negative performance.now() reading, got ` +
				`${String(pressT0Ms)}`
		);
	}
	return pressT0Ms;
}

function _parseCommand(message: unknown): PerformanceCommand {
	const record = _record(message);
	if (typeof record.type !== 'string') throw new TypeError('performance command type must be string');
	const type = record.type;
	if (type === 'crossfader' || type === 'master_volume' || type === 'headphone_mix' || type === 'headphone_level') {
		_exactKeys(record, ['type', 'value']);
		return { type, value: _unit('value', record.value) };
	}
	if (type === 'headphone_outputs_refresh') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'headphone_output_acquire') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'headphone_output_select') {
		_exactKeys(record, ['type', 'device_id']);
		if (typeof record.device_id !== 'string' || record.device_id.trim() === '') {
			throw new TypeError('device_id must be a non-empty string');
		}
		return { type, device_id: record.device_id };
	}
	const deck = _deck(record.deck);
	if (type === 'load') {
		_exactKeys(record, ['type', 'deck', 'stable_id']);
		if (typeof record.stable_id !== 'string' || record.stable_id.trim() === '') {
			throw new TypeError('stable_id must be a non-empty string');
		}
		return { type, deck, stable_id: record.stable_id };
	} else if (type === 'unload') {
		_exactKeys(record, ['type', 'deck']);
		return { type, deck };
	} else if (type === 'play') {
		_exactKeys(record, ['type', 'deck', 'playing']);
		return { type, deck, playing: _boolean('playing', record.playing) };
	} else if (type === 'cue' || type === 'master') {
		_exactKeys(record, ['type', 'deck']);
		return { type, deck };
	} else if (type === 'safety_loop_save' || type === 'safety_loop_clear') {
		_exactKeys(record, ['type', 'deck']);
		return { type, deck };
	} else if (type === 'safety_loop_arm') {
		_exactKeys(record, ['type', 'deck', 'armed']);
		return { type, deck, armed: _boolean('armed', record.armed) };
	} else if (type === 'key_sync') {
		_exactKeys(record, ['type', 'deck', 'enabled']);
		return { type, deck, enabled: _boolean('enabled', record.enabled) };
	} else if (type === 'seek') {
		_exactKeys(record, ['type', 'deck', 'position_ms']);
		const position_ms = _finite('position_ms', record.position_ms);
		if (position_ms < 0) throw new RangeError('position_ms must be >= 0');
		return { type, deck, position_ms };
	} else if (type === 'loop') {
		_exactKeys(record, ['type', 'deck', 'loop']);
		if (record.loop === null) return { type, deck, loop: null };
		const loop = _record(record.loop);
		_exactKeys(loop, ['in_ms', 'out_ms']);
		return {
			type,
			deck,
			loop: { in_ms: _finite('loop.in_ms', loop.in_ms), out_ms: _finite('loop.out_ms', loop.out_ms) }
		};
	} else if (type === 'beat_loop') {
		_exactKeys(record, ['type', 'deck', 'beats', 'start_ms']);
		const beats = _finite('beats', record.beats);
		if (!Number.isInteger(beats) || beats <= 0) {
			throw new RangeError(`beats must be a positive integer; got ${beats}`);
		}
		if (record.start_ms === undefined) return { type, deck, beats };
		const start_ms = _finite('start_ms', record.start_ms);
		if (start_ms < 0) throw new RangeError('start_ms must be >= 0');
		return { type, deck, beats, start_ms };
	} else if (type === 'tempo') {
		_exactKeys(record, ['type', 'deck', 'ratio']);
		return { type, deck, ratio: _finite('ratio', record.ratio) };
	} else if (type === 'pitch_range') {
		_exactKeys(record, ['type', 'deck', 'range']);
		if (record.range !== 8 && record.range !== 16 && record.range !== 100) {
			throw new RangeError(`range must be 8, 16, or 100; got ${String(record.range)}`);
		}
		return { type, deck, range: record.range };
	} else if (type === 'quantize' || type === 'beat_sync' || type === 'master_tempo' || type === 'slip' || type === 'channel_cue') {
		_exactKeys(record, ['type', 'deck', 'enabled']);
		return { type, deck, enabled: _boolean('enabled', record.enabled) };
	} else if (type === 'stem_mute') {
		_exactKeys(record, ['type', 'deck', 'stem', 'muted']);
		return { type, deck, stem: _stem(record.stem), muted: _boolean('muted', record.muted) };
	} else if (type === 'stem_solo') {
		_exactKeys(record, ['type', 'deck', 'stem', 'solo']);
		return { type, deck, stem: _stem(record.stem), solo: _boolean('solo', record.solo) };
	} else if (type === 'key_nudge') {
		_exactKeys(record, ['type', 'deck', 'semitones']);
		if (record.semitones !== -1 && record.semitones !== 1) {
			throw new RangeError(`semitones must be -1 or 1, got ${String(record.semitones)}`);
		}
		return { type, deck, semitones: record.semitones };
	} else if (type === 'sync_mode') {
		_exactKeys(record, ['type', 'deck', 'mode']);
		if (record.mode !== 'beat' && record.mode !== 'bar') {
			throw new TypeError(`mode must be "beat" or "bar"; got ${String(record.mode)}`);
		}
		return { type, deck, mode: record.mode };
	} else if (type === 'trim' || type === 'fader') {
		_exactKeys(record, ['type', 'deck', 'value']);
		return { type, deck, value: _unit('value', record.value) };
	} else if (type === 'eq') {
		_exactKeys(record, ['type', 'deck', 'band', 'value']);
		if (record.band !== 'low' && record.band !== 'mid' && record.band !== 'high') {
			throw new TypeError(`band must be low, mid, or high; got ${String(record.band)}`);
		}
		return { type, deck, band: record.band, value: _unit('value', record.value) };
	} else if (type === 'assign') {
		_exactKeys(record, ['type', 'deck', 'assign']);
		if (record.assign !== 'A' && record.assign !== 'B' && record.assign !== 'THRU') {
			throw new TypeError(`assign must be A, B, or THRU; got ${String(record.assign)}`);
		}
		return { type, deck, assign: record.assign };
	} else if (type === 'hot_cue_save') {
		_exactKeys(record, ['type', 'deck', 'slot', 'in_ms', 'revision']);
		const in_ms = _finite('in_ms', record.in_ms);
		if (!Number.isInteger(in_ms) || in_ms < 0) throw new RangeError('in_ms must be a non-negative integer');
		return { type, deck, slot: _hotCueSlot(record.slot), in_ms, revision: _revision('revision', record.revision) };
	} else if (type === 'hot_cue_clear') {
		_exactKeys(record, ['type', 'deck', 'slot', 'revision']);
		return { type, deck, slot: _hotCueSlot(record.slot), revision: _revision('revision', record.revision) };
	} else if (type === 'hot_cue_restore') {
		_exactKeys(record, ['type', 'deck', 'slot', 'revision', 'reversal_id']);
		return {
			type,
			deck,
			slot: _hotCueSlot(record.slot),
			revision: _revision('revision', record.revision),
			reversal_id: _revision('reversal_id', record.reversal_id)
		};
	}
	throw new TypeError(`unknown performance command type: ${type}`);
}

// --------------------------------------------------------------- state query

interface _BeatgridProjection {
	/** The ANLZ payload these arrays were derived from, by IDENTITY. */
	anlz: DeckState['anlz'];
	beatgrid: PerformanceDeckSnapshot['beatgrid'];
	beatgrid_ms: PerformanceDeckSnapshot['beatgrid_ms'];
}

/**
 * Per-deck memo of the beatgrid projections, keyed by ANLZ object identity.
 *
 * queryPerformanceState() runs after EVERY command, including the null-scope
 * continuous ones - trim, EQ, faders, crossfader - which fire once per
 * pointermove. It rebuilt all four decks' beatgrids each time: two full array
 * maps per deck, one of them allocating an object per beat. Measured at 3.9ms
 * p50 and 5.4ms worst with four decks loaded (2221 beats), i.e. 42-65% of the
 * main thread at a 120Hz pointer rate, spent rebuilding values that only change
 * on load. It never delayed the audio - that is a different bug, already fixed -
 * but it is what makes a knob drag feel heavy, and it gets worse as decks fill.
 *
 * Identity is the right key precisely because the engine REPLACES st.anlz
 * (`st.anlz = fresh`) rather than mutating it, so a reload or a hot-cue refresh
 * invalidates this for free and a stale grid cannot outlive its track.
 */
const _beatgridProjections: Record<DeckId, _BeatgridProjection | null> = {
	1: null,
	2: null,
	3: null,
	4: null
};

/** Deep-freeze the projection. The arrays are now SHARED across every snapshot
 * instead of rebuilt, so a consumer mutating one would poison every later read;
 * frozen, that attempt throws in strict mode instead of corrupting the cache
 * silently. These are immutable analysis values - nothing has cause to write
 * them - and the IPC payload is unchanged, since it crosses the boundary by
 * structured clone or JSON. */
function _freezeRows<T>(rows: T[]): T[] {
	for (const row of rows) Object.freeze(row);
	Object.freeze(rows);
	return rows;
}

function _beatgridProjection(deckId: DeckId, deck: DeckState): _BeatgridProjection {
	const cached = _beatgridProjections[deckId];
	if (cached !== null && cached.anlz === deck.anlz) return cached;
	const beats = deck.anlz?.beatgrid.beats ?? [];
	const fresh: _BeatgridProjection = {
		anlz: deck.anlz,
		beatgrid: _freezeRows(
			beats.map((beat) => ({ n: beat.n, bpm: beat.bpm, time_ms: beat.t * 1000 }))
		),
		beatgrid_ms: _freezeRows(beats.map((beat) => beat.t * 1000))
	};
	_beatgridProjections[deckId] = fresh;
	return fresh;
}

function _deckSnapshot(deckId: DeckId): PerformanceDeckSnapshot {
	const deck = getDeckState(deckId);
	const beatgrid = _beatgridProjection(deckId, deck);
	return {
		deck_id: deckId,
		stable_id: deck.stable_id,
		title: deck.title,
		artist: deck.artist,
		bpm: deck.bpm,
		key: deck.key,
		key_shift_semitones: deck.key_shift_semitones,
		effective_bpm: deckEffectiveBpm(deckId),
		duration_ms: deck.duration_ms,
		position_ms: deck.position_ms,
		playing: deck.playing,
		audible: deck.audible,
		transport_pending: deck.transport_pending,
		transport_clock: deckTransportClock(deckId),
		cue_ms: deck.cue_ms,
		pitch: deck.pitch,
		pitch_range: pitchRanges[deckId],
		quantize_enabled: deck.quantize_enabled,
		beat_sync_enabled: deck.beat_sync_enabled,
		key_sync_enabled: deck.key_sync_enabled,
		master_tempo_enabled: deck.master_tempo_enabled,
		slip_enabled: deck.slip_enabled,
		slip_active: deck.slip_active,
		slip_position_ms: deck.slip_position_ms,
		is_master: deck.is_master,
		sync_mode: deck.sync_mode,
		sync_error: deck.sync_error,
		processor_error: deck.processor_error,
		last_load_latency_ms: deck.last_load_latency_ms,
		last_load_stages:
			deck.last_load_stages === null ? null : { ...deck.last_load_stages },
		stems: {
			...deck.stems,
			alignment: deck.stems.alignment === null ? null : { ...deck.stems.alignment },
			controls: {
				vocal: { ...deck.stems.controls.vocal },
				instrumental: { ...deck.stems.controls.instrumental },
				drums: { ...deck.stems.controls.drums }
			}
		},
		loop: deck.loop === null ? null : { ...deck.loop },
		beatgrid: beatgrid.beatgrid,
		beatgrid_ms: beatgrid.beatgrid_ms,
		hot_cue_slots: (['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'] as HotCueSlot[]).map((slot) => ({
			slot,
			cue: deck.hot_cues.find((cue) => cue.slot === slot) ?? null,
			revision: deck.hot_cue_revisions[slot]
		})),
		hot_cue_reversal: hotCueReversals[deckId],
		command_error: performanceCommandStatus.deck_errors[deckId],
		command_pending: performanceCommandStatus.deck_pending[deckId] > 0
	};
}

export function queryPerformanceState(): PerformanceState {
	const masterDecks = DECK_IDS.filter((deck) => getDeckState(deck).is_master);
	if (masterDecks.length > 1) {
		throw new Error(`engine contract violation: ${masterDecks.length} master decks selected`);
	}
	return {
		version: 1,
		master_deck: masterDecks[0] ?? null,
		command_pending: performanceCommandStatus.active > 0 || performanceCommandStatus.queued > 0,
		command_queued: performanceCommandStatus.queued,
		decks: {
			1: _deckSnapshot(1),
			2: _deckSnapshot(2),
			3: _deckSnapshot(3),
			4: _deckSnapshot(4)
		},
		mixer: {
			crossfader: mixerState.crossfader,
			master: mixerState.master,
			headphones: {
				...mixerState.headphones,
				outputs: mixerState.headphones.outputs.map((output) => ({ ...output }))
			},
			channels: {
				1: { ...mixerState.channels[1] },
				2: { ...mixerState.channels[2] },
				3: { ...mixerState.channels[3] },
				4: { ...mixerState.channels[4] }
			}
		},
		preset: { ...performancePresetLifecycle },
		last_error: performanceCommandStatus.last_error
	};
}

// ----------------------------------------------------------- command routing

function _commandDeck(command: PerformanceCommand): DeckId | null {
	return 'deck' in command ? command.deck : null;
}

export function performanceCommandQueueScopes(
	command: PerformanceCommand
): readonly CommandScope[] | null {
	if (
		command.type === 'headphone_outputs_refresh' ||
		command.type === 'headphone_output_acquire' ||
		command.type === 'headphone_output_select'
	) {
		return ['headphone'];
	}
	const deck = _commandDeck(command);
	if (command.type === 'channel_cue') {
		if (deck === null) throw new Error('channel_cue has no deck command queue scope');
		return [deck];
	}
	if (
		command.type === 'trim' ||
		command.type === 'eq' ||
		command.type === 'fader' ||
		command.type === 'assign' ||
		command.type === 'crossfader' ||
		command.type === 'master_volume' ||
		command.type === 'headphone_mix' ||
		command.type === 'headphone_level'
	) {
		return null;
	}
	if (deck === null) throw new Error(`${command.type} has no command queue scope`);
	if (
		command.type === 'play' ||
		command.type === 'cue' ||
		command.type === 'seek' ||
		command.type === 'tempo' ||
		command.type === 'beat_sync' ||
		command.type === 'sync_mode' ||
		command.type === 'master' ||
		command.type === 'master_tempo' ||
		command.type === 'key_sync' ||
		command.type === 'key_nudge'
	) {
		return [deck, 'sync'];
	}
	return [deck];
}

function _errorMessage(error: unknown): string {
	return error instanceof Error ? `${error.name}: ${error.message}` : String(error);
}

/**
 * Q1 / S2: `pressT0Ms` is the input stamp on the `performance.now()` epoch, cut
 * at the UI boundary BEFORE the command can queue. It travels as an argument
 * rather than as a command field on purpose: `PerformanceCommand` is the
 * validated IPC wire shape (`_exactKeys` rejects unknown fields), and an
 * instrument has no business widening a public protocol.
 */
async function _execute(command: PerformanceCommand, pressT0Ms?: number): Promise<void> {
	if (command.type === 'load') {
		// THE deck-load entry point from every UI surface, so it is also the
		// one place the boot scheduler has to be told a load is in flight:
		// deferred boot work waits for this to settle rather than racing it
		// for the origin's six connections (PERF-R6). The beacon is a
		// counter, not a lock -- it cannot fail the load, and the scheduler
		// releases on its own ceiling if a load never settles.
		const deckLoadSettled = bootScheduler.deckLoadStarted();
		try {
			await engine.load(command.deck, command.stable_id);
		} finally {
			deckLoadSettled();
		}
		hotCueReversals[command.deck] = null;
		noteRecentDeck(command.deck);
	} else if (command.type === 'unload') {
		await engine.unload(command.deck);
		hotCueReversals[command.deck] = null;
	} else if (command.type === 'play') {
		if (command.playing) await engine.play(command.deck, pressT0Ms);
		else await engine.pause(command.deck, pressT0Ms);
		noteRecentDeck(command.deck);
	} else if (command.type === 'cue') {
		await engine.pressCue(command.deck, pressT0Ms);
		noteRecentDeck(command.deck);
	} else if (command.type === 'seek') {
		await engine.quantizedSeek(command.deck, command.position_ms);
	} else if (command.type === 'loop') {
		await engine.setLoop(command.deck, command.loop);
	} else if (command.type === 'beat_loop') {
		await engine.engageBeatLoop(command.deck, command.beats, command.start_ms);
	} else if (command.type === 'tempo') {
		await engine.setTempoRatio(command.deck, command.ratio);
	} else if (command.type === 'pitch_range') {
		engine.setPitchRange(command.deck, command.range);
	} else if (command.type === 'quantize') {
		engine.setQuantize(command.deck, command.enabled);
	} else if (command.type === 'beat_sync') {
		await engine.setBeatSync(command.deck, command.enabled);
	} else if (command.type === 'sync_mode') {
		await engine.setSyncMode(command.deck, command.mode);
	} else if (command.type === 'master') {
		await engine.setDeckMaster(command.deck);
	} else if (command.type === 'master_tempo') {
		await engine.setMasterTempo(command.deck, command.enabled);
	} else if (command.type === 'stem_mute') {
		engine.setStemMute(command.deck, command.stem, command.muted);
	} else if (command.type === 'stem_solo') {
		engine.setStemSolo(command.deck, command.stem, command.solo);
	} else if (command.type === 'slip') {
		await engine.setSlip(command.deck, command.enabled);
	} else if (command.type === 'key_sync') {
		await engine.setKeySync(command.deck, command.enabled);
	} else if (command.type === 'key_nudge') {
		await engine.nudgeKey(command.deck, command.semitones);
	} else if (command.type === 'trim') {
		engine.setTrim(command.deck, command.value);
	} else if (command.type === 'eq') {
		engine.setEq(command.deck, command.band, command.value);
	} else if (command.type === 'fader') {
		engine.setFader(command.deck, command.value);
		} else if (command.type === 'assign') {
		engine.assignChannel(command.deck, command.assign);
	} else if (command.type === 'channel_cue') {
		engine.setChannelCue(command.deck, command.enabled);
	} else if (command.type === 'crossfader') {
		engine.setCrossfader(command.value);
	} else if (command.type === 'master_volume') {
		engine.setMaster(command.value);
	} else if (command.type === 'headphone_mix') {
		engine.setHeadphoneMix(command.value);
	} else if (command.type === 'headphone_level') {
		engine.setHeadphoneLevel(command.value);
	} else if (command.type === 'headphone_outputs_refresh') {
		await engine.refreshHeadphoneOutputs();
	} else if (command.type === 'headphone_output_acquire') {
		await engine.acquireHeadphoneOutput();
	} else if (command.type === 'headphone_output_select') {
		await engine.selectHeadphoneOutput(command.device_id);
	} else if (command.type === 'safety_loop_save') {
		// Engine-side and synchronous: it captures the deck's currently
		// engaged loop, and throws when there is none to capture.
		engine.saveSafetyLoop(command.deck);
	} else if (command.type === 'safety_loop_arm') {
		engine.setSafetyLoopArmed(command.deck, command.armed);
	} else if (command.type === 'safety_loop_clear') {
		engine.clearSafetyLoop(command.deck);
	} else if (command.type === 'hot_cue_save') {
		const stableId = _hotCueDriver.stableId(command.deck);
		if (stableId === null) throw new Error(`hot cue ${command.slot}: deck is not loaded`);
		const result = await saveHotCue(stableId, command.slot, command.in_ms, command.revision);
		if (result.reversal === undefined) throw new Error(`hot cue ${command.slot}: server omitted reversal token`);
		hotCueReversals[command.deck] = {
			slot: command.slot,
			revision: result.revision,
			reversal_id: result.reversal.reversal_id
		};
		await _hotCueDriver.refresh(command.deck);
	} else if (command.type === 'hot_cue_clear') {
		const stableId = _hotCueDriver.stableId(command.deck);
		if (stableId === null) throw new Error(`hot cue ${command.slot}: deck is not loaded`);
		const result = await clearHotCue(stableId, command.slot, command.revision);
		if (result.reversal === undefined) throw new Error(`hot cue ${command.slot}: server omitted reversal token`);
		hotCueReversals[command.deck] = {
			slot: command.slot,
			revision: result.revision,
			reversal_id: result.reversal.reversal_id
		};
		await _hotCueDriver.refresh(command.deck);
	} else if (command.type === 'hot_cue_restore') {
		const stableId = _hotCueDriver.stableId(command.deck);
		if (stableId === null) throw new Error(`hot cue ${command.slot}: deck is not loaded`);
		await restoreHotCue(stableId, command.slot, command.revision, command.reversal_id);
		hotCueReversals[command.deck] = null;
		await _hotCueDriver.refresh(command.deck);
	} else {
		const _exhaustive: never = command;
		throw new Error(`Unhandled performance command: ${JSON.stringify(_exhaustive)}`);
	}
}

function _persistCommandError(deck: DeckId | null, error: unknown): void {
	const messageText = _errorMessage(error);
	performanceCommandStatus.last_error = messageText;
	if (deck !== null) performanceCommandStatus.deck_errors[deck] = messageText;
	pushToast(`Performance command failed - ${messageText}`, 'error');
}

/**
 * Clear the visible error on one deck.
 *
 * The banner used to be clearable only as a side effect of the next command
 * that happened to touch the same deck, so an error from a step nothing would
 * retry (AutoPlay's master handover) stuck to the deck for the rest of the
 * set with no way to get rid of it. Operator-driven dismissal is the fix; the
 * underlying condition is unchanged and will re-report if it recurs.
 */
export function dismissPerformanceDeckError(deck: DeckId): void {
	performanceCommandStatus.deck_errors[deck] = null;
	const st = getDeckState(deck);
	st.sync_error = null;
	st.processor_error = null;
}

function _resetCommandStatus(): void {
	performanceCommandStatus.last_error = null;
	performanceCommandStatus.active = 0;
	performanceCommandStatus.queued = 0;
	for (const deck of DECK_IDS) {
		performanceCommandStatus.deck_errors[deck] = null;
		performanceCommandStatus.deck_pending[deck] = 0;
	}
}

function _commandSessionError(generation: number): ScopedCommandInvalidatedError {
	return new ScopedCommandInvalidatedError(
		`performance command session ${generation} was invalidated`
	);
}

function _assertCommandSession(generation: number): void {
	if (_activeCommandSession?.generation !== generation) {
		throw _commandSessionError(generation);
	}
}

function _commandSessionIsCurrent(generation: number): boolean {
	return _activeCommandSession?.generation === generation;
}

function _startCommandSession(): number {
	if (_activeCommandSession !== null) {
		throw new Error(
			`performance command session ${_activeCommandSession.generation} is already active`
		);
	}
	_commandGeneration += 1;
	_activeCommandSession = { generation: _commandGeneration };
	return _commandGeneration;
}

function _invalidateCommandSession(generation: number): void {
	_assertCommandSession(generation);
	_activeCommandSession = null;
	_commandStatusGeneration += 1;
	_commandScheduler.invalidateQueued(`performance command session ${generation} was invalidated`);
	_resetCommandStatus();
}

function _currentCommandSession(): number {
	const session = _activeCommandSession;
	if (session === null) throw _commandSessionError(_commandGeneration);
	return session.generation;
}

function _assertPresetId(id: string): void {
	if (typeof id !== 'string' || id.trim() === '') {
		throw new TypeError('performance preset transaction id must be a non-empty string');
	}
}

function _setPresetPhase(id: string, phase: PerformancePresetPhase): void {
	if (_presetClaim?.id !== id) {
		throw new Error(`performance preset ${id} does not own the lifecycle lock`);
	}
	performancePresetLifecycle.phase = phase;
}

function _releasePreset(id: string, phase: 'idle' | 'ready' | 'error', error: string | null): void {
	if (_presetClaim?.id !== id) {
		throw new Error(`performance preset ${id} cannot release an unowned lifecycle lock`);
	}
	_presetClaim = null;
	performancePresetLifecycle.phase = phase;
	performancePresetLifecycle.active = false;
	performancePresetLifecycle.error = error;
	if (phase === 'idle') performancePresetLifecycle.id = null;
}

async function _dispatchWithinPreset(command: PerformanceCommand): Promise<PerformanceState> {
	const deck = _commandDeck(command);
	if (deck !== null) performanceCommandStatus.deck_errors[deck] = null;
	try {
		await _execute(command);
	} catch (error) {
		_persistCommandError(deck, error);
		throw error;
	}
	return queryPerformanceState();
}

function _enqueuePresetPhase<T>(
	id: string,
	work: (driver: PerformancePresetTransactionDriver) => Promise<T>
): Promise<T> {
	const statusGeneration = _commandStatusGeneration;
	let started = false;
	performanceCommandStatus.queued += 1;
	for (const deck of DECK_IDS) performanceCommandStatus.deck_pending[deck] += 1;
	const run = async (): Promise<T> => {
		if (statusGeneration !== _commandStatusGeneration) {
			throw _commandSessionError(_commandGeneration);
		}
		started = true;
		performanceCommandStatus.queued -= 1;
		performanceCommandStatus.active += 1;
		performanceCommandStatus.last_error = null;
		for (const deck of DECK_IDS) performanceCommandStatus.deck_errors[deck] = null;
		try {
			return await work({
				dispatch: _dispatchWithinPreset,
				query: queryPerformanceState,
				setPhase: (phase) => _setPresetPhase(id, phase)
			});
		} finally {
			if (statusGeneration === _commandStatusGeneration) {
				performanceCommandStatus.active -= 1;
				for (const deck of DECK_IDS) performanceCommandStatus.deck_pending[deck] -= 1;
			}
		}
	};
	return _commandScheduler.run(PERFORMANCE_PRESET_COMMAND_SCOPES, run).finally(() => {
		if (!started && statusGeneration === _commandStatusGeneration) {
			performanceCommandStatus.queued -= 1;
			for (const deck of DECK_IDS) performanceCommandStatus.deck_pending[deck] -= 1;
		}
	});
}

function _recordPresetFailure(id: string, error: unknown): void {
	const message = _errorMessage(error);
	if (performanceCommandStatus.last_error !== message) _persistCommandError(null, error);
	_releasePreset(id, 'error', message);
}

export async function preparePerformancePresetTransaction<T>(
	id: string,
	work: (driver: PerformancePresetTransactionDriver) => Promise<T>
): Promise<T> {
	_assertPresetId(id);
	if (_presetClaim !== null) {
		throw new Error(
			`performance preset ${_presetClaim.id} already owns the lifecycle lock at ` +
				`${performancePresetLifecycle.phase}`
		);
	}
	_presetClaim = { id };
	performancePresetLifecycle.id = id;
	performancePresetLifecycle.phase = 'queued';
	performancePresetLifecycle.active = true;
	performancePresetLifecycle.error = null;
	try {
		const result = await _enqueuePresetPhase(id, work);
		performancePresetLifecycle.phase = 'awaiting_audio';
		return result;
	} catch (error) {
		_recordPresetFailure(id, error);
		throw error;
	}
}

export async function startPerformancePresetTransaction<T>(
	id: string,
	work: (driver: PerformancePresetTransactionDriver) => Promise<T>
): Promise<T> {
	_assertPresetId(id);
	if (_presetClaim?.id !== id || performancePresetLifecycle.phase !== 'awaiting_audio') {
		throw new Error(`performance preset ${id} is not prepared and awaiting audio activation`);
	}
	performancePresetLifecycle.phase = 'starting';
	try {
		const result = await _enqueuePresetPhase(id, work);
		const state = queryPerformanceState();
		if (state.command_pending || state.command_queued !== 0) {
			throw new Error(`performance preset ${id} start returned before the command queue became idle`);
		}
		_releasePreset(id, 'ready', null);
		return result;
	} catch (error) {
		_recordPresetFailure(id, error);
		throw error;
	}
}

export async function stopPerformancePresetTransaction<T>(
	id: string,
	work: (driver: PerformancePresetTransactionDriver) => Promise<T>
): Promise<T> {
	_assertPresetId(id);
	if (_presetClaim !== null) {
		throw new Error(
			`performance preset ${_presetClaim.id} already owns the lifecycle lock at ` +
				`${performancePresetLifecycle.phase}`
		);
	}
	if (performancePresetLifecycle.id !== id || performancePresetLifecycle.phase !== 'ready') {
		throw new Error(`performance preset ${id} is not ready for stop cleanup`);
	}
	_presetClaim = { id };
	performancePresetLifecycle.phase = 'queued';
	performancePresetLifecycle.active = true;
	performancePresetLifecycle.error = null;
	try {
		const result = await _enqueuePresetPhase(id, work);
		const state = queryPerformanceState();
		if (state.command_pending || state.command_queued !== 0) {
			throw new Error(`performance preset ${id} stop returned before the command queue became idle`);
		}
		_releasePreset(id, 'idle', null);
		return result;
	} catch (error) {
		_recordPresetFailure(id, error);
		throw error;
	}
}

export function abortPreparedPerformancePreset(id: string, reason: string): void {
	_assertPresetId(id);
	if (_presetClaim?.id !== id || performancePresetLifecycle.phase !== 'awaiting_audio') return;
	const error = new Error(`performance preset ${id} aborted: ${reason}`);
	_persistCommandError(null, error);
	_releasePreset(id, 'error', _errorMessage(error));
}

async function _dispatchUnknown(
	message: unknown,
	commandGeneration: number,
	pressT0Ms?: number
): Promise<PerformanceState> {
	_assertCommandSession(commandGeneration);
	let command: PerformanceCommand;
	try {
		command = _parseCommand(message);
	} catch (error) {
		_persistCommandError(null, error);
		throw error;
	}
	const deck = _commandDeck(command);
	if (_presetClaim !== null) {
		const error = new Error(
			`performance preset ${_presetClaim.id} owns controls at ${performancePresetLifecycle.phase}; ` +
				`command ${command.type} rejected`
		);
		_persistCommandError(deck, error);
		throw error;
	}
	const scopes = performanceCommandQueueScopes(command);
	if (command.type === 'headphone_output_acquire') {
		performanceCommandStatus.active += 1;
		performanceCommandStatus.last_error = null;
		try {
			const acquired = _commandScheduler.runImmediatelyIfIdle('headphone', async () => {
				_assertCommandSession(commandGeneration);
				await _execute(command, pressT0Ms);
			});
			await acquired;
			_assertCommandSession(commandGeneration);
			return queryPerformanceState();
		} catch (error) {
			if (_commandSessionIsCurrent(commandGeneration)) _persistCommandError(null, error);
			throw error;
		} finally {
			if (_commandSessionIsCurrent(commandGeneration)) performanceCommandStatus.active -= 1;
		}
	}
	if (scopes === null) {
		performanceCommandStatus.last_error = null;
		if (deck !== null) performanceCommandStatus.deck_errors[deck] = null;
		try {
			_assertCommandSession(commandGeneration);
			await _execute(command, pressT0Ms);
			_assertCommandSession(commandGeneration);
			return queryPerformanceState();
		} catch (error) {
			if (_commandSessionIsCurrent(commandGeneration)) _persistCommandError(deck, error);
			throw error;
		}
	}
	performanceCommandStatus.queued += 1;
	if (deck !== null) performanceCommandStatus.deck_pending[deck] += 1;
	let started = false;

	const run = async (): Promise<PerformanceState> => {
		_assertCommandSession(commandGeneration);
		started = true;
		performanceCommandStatus.queued -= 1;
		performanceCommandStatus.active += 1;
		performanceCommandStatus.last_error = null;
		if (deck !== null) performanceCommandStatus.deck_errors[deck] = null;
		try {
			// Q1: this body starts only AFTER the scope wait above, which is
			// exactly the gap press_to_schedule_ms exists to expose.
			await _execute(command, pressT0Ms);
			_assertCommandSession(commandGeneration);
			return queryPerformanceState();
		} catch (error) {
			if (_commandSessionIsCurrent(commandGeneration)) _persistCommandError(deck, error);
			throw error;
		} finally {
			if (_commandSessionIsCurrent(commandGeneration)) {
				performanceCommandStatus.active -= 1;
				if (deck !== null) performanceCommandStatus.deck_pending[deck] -= 1;
			}
		}
	};

	return _commandScheduler.run(scopes, run).finally(() => {
		if (!started && _commandSessionIsCurrent(commandGeneration)) {
			performanceCommandStatus.queued -= 1;
			if (deck !== null) performanceCommandStatus.deck_pending[deck] -= 1;
		}
	});
}

export async function dispatchPerformanceCommand(
	command: PerformanceCommand,
	pressT0Ms?: number
): Promise<PerformanceState> {
	return _dispatchUnknown(command, _currentCommandSession(), pressT0Ms);
}

/**
 * UI event boundary: await the same fail-fast dispatcher, then consume the
 * rejection only after it has been persisted in visible reactive state.
 *
 * Q1 / S2: this is where the press clock starts. The default is taken on ENTRY,
 * before `_dispatchUnknown` can park the command behind another scope's tail,
 * so `press_to_schedule_ms` contains that wait rather than starting after it.
 * A caller holding the originating DOM event should pass `event.timeStamp`,
 * which is already on the `performance.now()` epoch and is earlier still.
 */
export async function runPerformanceCommandFromUi(
	command: PerformanceCommand,
	pressT0Ms: number = performance.now()
): Promise<void> {
	try {
		await dispatchPerformanceCommand(command, pressT0Ms);
	} catch {
		// The dispatcher already populated the deck alert and toast.
	}
}

function _captureUnknown(deck: unknown): DeckAudioSnapshot {
	const snapshot = engine.captureDeckAudio(_deck(deck));
	const scalars = [snapshot.context_time_s, snapshot.sample_rate_hz, snapshot.fft_size];
	const values = [...scalars, ...snapshot.frequency_db, ...snapshot.time_domain];
	if (!values.every((value) => Number.isFinite(value))) {
		throw new Error('deck audio capture contains non-finite values and cannot be serialized');
	}
	if (snapshot.sample_rate_hz <= 0 || !Number.isInteger(snapshot.fft_size) || snapshot.fft_size <= 0) {
		throw new Error('deck audio capture has invalid sample-rate or FFT metadata');
	}
	return {
		context_time_s: snapshot.context_time_s,
		sample_rate_hz: snapshot.sample_rate_hz,
		fft_size: snapshot.fft_size,
		frequency_db: [...snapshot.frequency_db],
		time_domain: [...snapshot.time_domain]
	};
}

export function installPerformanceBrowserIpc(): () => void {
	if (typeof window === 'undefined') throw new Error('performance IPC requires a browser window');
	if (window.musicDjToolsPerformance !== undefined) {
		throw new Error('performance IPC is already installed');
	}
	const commandGeneration = _startCommandSession();
	const ipc: PerformanceBrowserIpc = Object.freeze({
		version: 1 as const,
		dispatch: (message: unknown, pressT0Ms?: number) =>
			_dispatchUnknown(message, commandGeneration, _validatedPressStamp(pressT0Ms)),
		query: () => {
			_assertCommandSession(commandGeneration);
			return queryPerformanceState();
		},
		capture: (deck: unknown) => {
			_assertCommandSession(commandGeneration);
			return _captureUnknown(deck);
		},
		dismissDeckError: (deck: unknown) => {
			_assertCommandSession(commandGeneration);
			dismissPerformanceDeckError(_deck(deck));
			return queryPerformanceState();
		}
	});
	window.musicDjToolsPerformance = ipc;
	return () => {
		if (window.musicDjToolsPerformance !== ipc) {
			throw new Error('performance IPC ownership changed before cleanup');
		}
		_invalidateCommandSession(commandGeneration);
		delete window.musicDjToolsPerformance;
	};
}
