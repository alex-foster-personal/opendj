/**
 * Typed, fail-fast browser command surface for /performance.
 *
 * UI controls and browser automation both dispatch through the same command
 * function. Runtime validation rejects malformed IPC messages, while command
 * failures are recorded in reactive state and shown by each deck.
 */

import { pushToast } from '$lib/stores.svelte';
import {
	DECK_IDS,
	deckEffectiveBpm,
	engine,
	getDeckState,
	mixerState,
	pitchRanges,
	type PitchRange
} from '$lib/rb/audio-engine.svelte';
import type {
	CrossfaderAssign,
	DeckAudioSnapshot,
	DeckId,
	EqBand,
	LoopState,
	SyncMode
} from '$lib/rb/types';

export type PerformanceCommand =
	| { type: 'load'; deck: DeckId; stable_id: string }
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
	| { type: 'trim'; deck: DeckId; value: number }
	| { type: 'eq'; deck: DeckId; band: EqBand; value: number }
	| { type: 'fader'; deck: DeckId; value: number }
	| { type: 'assign'; deck: DeckId; assign: CrossfaderAssign }
	| { type: 'crossfader'; value: number }
	| { type: 'master_volume'; value: number };

export interface PerformanceDeckSnapshot {
	deck_id: DeckId;
	stable_id: string | null;
	title: string | null;
	artist: string | null;
	bpm: number | null;
	effective_bpm: number | null;
	duration_ms: number | null;
	position_ms: number;
	playing: boolean;
	audible: boolean;
	transport_pending: boolean;
	cue_ms: number | null;
	pitch: number;
	pitch_range: PitchRange;
	quantize_enabled: boolean;
	beat_sync_enabled: boolean;
	master_tempo_enabled: boolean;
	is_master: boolean;
	sync_mode: SyncMode;
	sync_error: string | null;
	processor_error: string | null;
	loop: LoopState | null;
	beatgrid_ms: number[];
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
	};
	last_error: string | null;
}

export interface PerformanceBrowserIpc {
	readonly version: 1;
	dispatch(message: unknown): Promise<PerformanceState>;
	query(): PerformanceState;
	capture(deck: unknown): DeckAudioSnapshot;
}

export const performanceCommandStatus: {
	last_error: string | null;
	deck_errors: Record<DeckId, string | null>;
	deck_pending: Record<DeckId, number>;
	active: boolean;
	queued: number;
} = $state({
	last_error: null,
	deck_errors: { 1: null, 2: null, 3: null, 4: null },
	deck_pending: { 1: 0, 2: 0, 3: 0, 4: 0 },
	active: false,
	queued: 0
});

let _commandQueue: Promise<void> = Promise.resolve();

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

function _parseCommand(message: unknown): PerformanceCommand {
	const record = _record(message);
	if (typeof record.type !== 'string') throw new TypeError('performance command type must be string');
	const type = record.type;
	if (type === 'crossfader' || type === 'master_volume') {
		_exactKeys(record, ['type', 'value']);
		return { type, value: _unit('value', record.value) };
	}
	const deck = _deck(record.deck);
	if (type === 'load') {
		_exactKeys(record, ['type', 'deck', 'stable_id']);
		if (typeof record.stable_id !== 'string' || record.stable_id.trim() === '') {
			throw new TypeError('stable_id must be a non-empty string');
		}
		return { type, deck, stable_id: record.stable_id };
	} else if (type === 'play') {
		_exactKeys(record, ['type', 'deck', 'playing']);
		return { type, deck, playing: _boolean('playing', record.playing) };
	} else if (type === 'cue' || type === 'master') {
		_exactKeys(record, ['type', 'deck']);
		return { type, deck };
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
	} else if (type === 'quantize' || type === 'beat_sync' || type === 'master_tempo') {
		_exactKeys(record, ['type', 'deck', 'enabled']);
		return { type, deck, enabled: _boolean('enabled', record.enabled) };
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
	}
	throw new TypeError(`unknown performance command type: ${type}`);
}

// --------------------------------------------------------------- state query

function _deckSnapshot(deckId: DeckId): PerformanceDeckSnapshot {
	const deck = getDeckState(deckId);
	return {
		deck_id: deckId,
		stable_id: deck.stable_id,
		title: deck.title,
		artist: deck.artist,
		bpm: deck.bpm,
		effective_bpm: deckEffectiveBpm(deckId),
		duration_ms: deck.duration_ms,
		position_ms: deck.position_ms,
		playing: deck.playing,
		audible: deck.audible,
		transport_pending: deck.transport_pending,
		cue_ms: deck.cue_ms,
		pitch: deck.pitch,
		pitch_range: pitchRanges[deckId],
		quantize_enabled: deck.quantize_enabled,
		beat_sync_enabled: deck.beat_sync_enabled,
		master_tempo_enabled: deck.master_tempo_enabled,
		is_master: deck.is_master,
		sync_mode: deck.sync_mode,
		sync_error: deck.sync_error,
		processor_error: deck.processor_error,
		loop: deck.loop === null ? null : { ...deck.loop },
		beatgrid_ms: deck.anlz?.beatgrid.beats.map((beat) => beat.t * 1000) ?? [],
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
		command_pending: performanceCommandStatus.active || performanceCommandStatus.queued > 0,
		command_queued: performanceCommandStatus.queued,
		decks: {
			1: _deckSnapshot(1),
			2: _deckSnapshot(2),
			3: _deckSnapshot(3),
			4: _deckSnapshot(4)
		},
		mixer: { crossfader: mixerState.crossfader, master: mixerState.master },
		last_error: performanceCommandStatus.last_error
	};
}

// ----------------------------------------------------------- command routing

function _commandDeck(command: PerformanceCommand): DeckId | null {
	return 'deck' in command ? command.deck : null;
}

function _errorMessage(error: unknown): string {
	return error instanceof Error ? `${error.name}: ${error.message}` : String(error);
}

async function _execute(command: PerformanceCommand): Promise<void> {
	if (command.type === 'load') {
		await engine.load(command.deck, command.stable_id);
	} else if (command.type === 'play') {
		if (command.playing) await engine.play(command.deck);
		else await engine.pause(command.deck);
	} else if (command.type === 'cue') {
		await engine.pressCue(command.deck);
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
	} else if (command.type === 'trim') {
		engine.setTrim(command.deck, command.value);
	} else if (command.type === 'eq') {
		engine.setEq(command.deck, command.band, command.value);
	} else if (command.type === 'fader') {
		engine.setFader(command.deck, command.value);
	} else if (command.type === 'assign') {
		engine.assignChannel(command.deck, command.assign);
	} else if (command.type === 'crossfader') {
		engine.setCrossfader(command.value);
	} else if (command.type === 'master_volume') {
		engine.setMaster(command.value);
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

async function _dispatchUnknown(message: unknown): Promise<PerformanceState> {
	let command: PerformanceCommand;
	try {
		command = _parseCommand(message);
	} catch (error) {
		_persistCommandError(null, error);
		throw error;
	}
	const deck = _commandDeck(command);
	performanceCommandStatus.queued += 1;
	if (deck !== null) performanceCommandStatus.deck_pending[deck] += 1;

	const run = async (): Promise<PerformanceState> => {
		performanceCommandStatus.queued -= 1;
		performanceCommandStatus.active = true;
		performanceCommandStatus.last_error = null;
		if (deck !== null) performanceCommandStatus.deck_errors[deck] = null;
		try {
			await _execute(command);
		} catch (error) {
			_persistCommandError(deck, error);
			throw error;
		} finally {
			performanceCommandStatus.active = false;
			if (deck !== null) performanceCommandStatus.deck_pending[deck] -= 1;
		}
		try {
			return queryPerformanceState();
		} catch (error) {
			_persistCommandError(deck, error);
			throw error;
		}
	};

	const scheduled = _commandQueue.then(run);
	_commandQueue = scheduled.then(
		() => undefined,
		() => undefined
	);
	return scheduled;
}

export async function dispatchPerformanceCommand(
	command: PerformanceCommand
): Promise<PerformanceState> {
	return _dispatchUnknown(command);
}

/** UI event boundary: await the same fail-fast dispatcher, then consume the
 * rejection only after it has been persisted in visible reactive state. */
export async function runPerformanceCommandFromUi(command: PerformanceCommand): Promise<void> {
	try {
		await dispatchPerformanceCommand(command);
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
	const ipc: PerformanceBrowserIpc = Object.freeze({
		version: 1 as const,
		dispatch: _dispatchUnknown,
		query: queryPerformanceState,
		capture: _captureUnknown
	});
	window.musicDjToolsPerformance = ipc;
	return () => {
		if (window.musicDjToolsPerformance !== ipc) {
			throw new Error('performance IPC ownership changed before cleanup');
		}
		delete window.musicDjToolsPerformance;
	};
}
