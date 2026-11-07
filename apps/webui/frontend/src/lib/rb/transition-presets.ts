/**
 * Transition presets (#9456, epic #9373 D5): named multi-control moves planned on the
 * master beat grid and run via a host-injected scheduler/writer.
 *
 * commanded-move.ts (D1) and commanded-move-runner.svelte.ts (D3) are not on main yet;
 * per-move planning lives in `_planMove` until #9452 lands. Hand take-over and
 * touch-cancel are owned by D3 (#9454).
 */

import type { DeckId } from '$lib/rb/deck-id';
import { durationProgress, resolveDuration } from '$lib/rb/agent-duration';

export type PresetRole = 'outgoing' | 'incoming';
export type PresetControl = 'low' | 'mid' | 'high' | 'filter' | 'fader';
export type PresetControlRef = `${PresetRole}.${PresetControl}`;

export interface PresetMove {
	control: PresetControlRef;
	to: number;
	startBeat?: number;
}

export interface TransitionPreset {
	id: string;
	label: string;
	beats: number;
	moves: readonly PresetMove[];
}

export interface PresetDeckSnapshot {
	id: DeckId;
	loaded: boolean;
	playing: boolean;
	isMaster: boolean;
	position_ms: number;
	beatgrid: readonly { n: number; time_ms: number }[];
	controls: Record<PresetControl, number>;
}

export interface PresetDecksInput {
	decks: readonly PresetDeckSnapshot[];
	incomingHint?: DeckId | null;
	outgoing?: DeckId;
	incoming?: DeckId;
}

/** Mixer write variants; structurally eq/filter/fader PerformanceCommand arms. */
export type PresetWrite =
	| { type: 'eq'; deck: DeckId; band: 'low' | 'mid' | 'high'; value: number }
	| { type: 'filter'; deck: DeckId; value: number }
	| { type: 'fader'; deck: DeckId; value: number };

/**
 * Production adapter: queryPerformanceState -> snapshots, dispatchPerformanceCommand
 * as write, ~16 ms schedule. Must not use setKnobAbsolute/nudgeKnob (silent on unmounted).
 */
export interface TransitionPresetHost {
	readDecks(): PresetDecksInput;
	write(command: PresetWrite): void;
	schedule(tick: () => void): () => void;
}

export type TransitionPresetErrorCode =
	| 'unknown_preset'
	| 'no_master_deck'
	| 'needs_two_playing_decks'
	| 'ambiguous_incoming_deck'
	| 'no_grid'
	| 'no_host';

export class TransitionPresetError extends Error {
	readonly code: TransitionPresetErrorCode;

	constructor(code: TransitionPresetErrorCode, message: string) {
		super(message);
		this.name = 'TransitionPresetError';
		this.code = code;
	}
}

// Values: (private path),
// 03-filter-lane-transitions.md, 01-volume-lane-transitions.md; apps/autodj/styles.py.
// bass-swap: swap on the last whole beat (startBeat 7 of 8); grid has no half beats.
export const TRANSITION_PRESETS: readonly TransitionPreset[] = [
	{
		id: 'bass-swap-8',
		label: 'Bass swap (8 beats)',
		beats: 8,
		moves: [
			{ control: 'outgoing.low', to: 0.0, startBeat: 7 },
			{ control: 'incoming.low', to: 0.5, startBeat: 7 }
		]
	},
	{
		id: 'filter-out-16',
		label: 'Filter out (16 beats)',
		beats: 16,
		moves: [{ control: 'outgoing.filter', to: 0.85 }]
	},
	{
		id: 'fader-blend-16',
		label: 'Fader blend (16 beats)',
		beats: 16,
		moves: [
			{ control: 'outgoing.fader', to: 0.0 },
			{ control: 'incoming.fader', to: 1.0 }
		]
	}
] as const;

export type TransitionPresetId = (typeof TRANSITION_PRESETS)[number]['id'];

export interface PresetMovePlan {
	key: string;
	deck: DeckId;
	control: PresetControl;
	from: number;
	to: number;
	startMs: number;
	endMs: number;
	valueAt: (tMs: number) => number;
}

export interface PresetPlan {
	presetId: string;
	outgoing: DeckId;
	incoming: DeckId;
	clockDeck: DeckId;
	startMs: number;
	endMs: number;
	moves: readonly PresetMovePlan[];
}

export type TransitionPresetRunResult =
	| { status: 'started'; plan: PresetPlan; done: Promise<'completed' | 'aborted'> }
	| { status: 'aborted'; presetId: string };

interface ActiveRun {
	aborted: boolean;
	cancelSchedule: () => void;
	plan: PresetPlan;
	done: Promise<'completed' | 'aborted'>;
	resolveDone: (outcome: 'completed' | 'aborted') => void;
	lastWritten: Map<string, number>;
	host: TransitionPresetHost;
}

const _active = new Map<string, ActiveRun>();

function _clamp01(value: number): number {
	if (!Number.isFinite(value)) return 0;
	return Math.min(1, Math.max(0, value));
}

function _parseControlRef(ref: PresetControlRef): { role: PresetRole; control: PresetControl } {
	const [role, control] = ref.split('.') as [PresetRole, PresetControl];
	if ((role !== 'outgoing' && role !== 'incoming') || !control) {
		throw new TransitionPresetError('unknown_preset', `invalid control ref: ${ref}`);
	}
	return { role, control };
}

function _validatePreset(preset: TransitionPreset): void {
	for (const move of preset.moves) {
		if (!Number.isFinite(move.to) || move.to < 0 || move.to > 1) {
			throw new TransitionPresetError('unknown_preset', `${preset.id}: to must be 0..1`);
		}
		if (move.startBeat !== undefined) {
			if (!Number.isInteger(move.startBeat) || move.startBeat < 0 || move.startBeat >= preset.beats) {
				throw new TransitionPresetError(
					'unknown_preset',
					`${preset.id}: startBeat must be in [0, ${preset.beats})`
				);
			}
		}
	}
}

for (const preset of TRANSITION_PRESETS) {
	_validatePreset(preset);
}

function _deckById(input: PresetDecksInput, id: DeckId): PresetDeckSnapshot | undefined {
	return input.decks.find((d) => d.id === id);
}

export function resolvePresetDecks(
	input: PresetDecksInput,
	presetLabel?: string
): { outgoing: DeckId; incoming: DeckId } {
	if (input.outgoing !== undefined && input.incoming !== undefined) {
		if (input.outgoing === input.incoming) {
			throw new TransitionPresetError('needs_two_playing_decks', 'outgoing and incoming must differ');
		}
		const out = _deckById(input, input.outgoing);
		const inc = _deckById(input, input.incoming);
		if (!out?.loaded || !out.playing || !inc?.loaded || !inc.playing) {
			throw new TransitionPresetError(
				'needs_two_playing_decks',
				presetLabel ? `${presetLabel} needs two playing decks` : 'needs two playing decks'
			);
		}
		return { outgoing: input.outgoing, incoming: input.incoming };
	}

	const masters = input.decks.filter((d) => d.isMaster && d.playing && d.loaded);
	if (masters.length === 0) {
		throw new TransitionPresetError('no_master_deck', 'no playing master deck');
	}
	if (masters.length > 1) {
		throw new TransitionPresetError('no_master_deck', 'ambiguous master deck');
	}
	const outgoing = masters[0].id;
	const candidates = input.decks.filter(
		(d) => d.id !== outgoing && d.loaded && d.playing
	);
	if (candidates.length === 0) {
		throw new TransitionPresetError(
			'needs_two_playing_decks',
			presetLabel ? `${presetLabel} needs a second playing deck` : 'needs a second playing deck'
		);
	}
	if (candidates.length === 1) {
		return { outgoing, incoming: candidates[0].id };
	}
	if (input.incomingHint != null && candidates.some((d) => d.id === input.incomingHint)) {
		return { outgoing, incoming: input.incomingHint };
	}
	throw new TransitionPresetError('ambiguous_incoming_deck', 'multiple playing decks; set incomingHint');
}

// Swap for planCommandedMove (lib/rb/commanded-move.ts) when #9452 lands.
function _planMove(
	deck: DeckId,
	control: PresetControl,
	from: number,
	to: number,
	startMs: number,
	endMs: number
): PresetMovePlan {
	const plan = { start_ms: startMs, target_ms: endMs };
	return {
		key: `${deck}:${control}`,
		deck,
		control,
		from,
		to,
		startMs,
		endMs,
		valueAt: (tMs: number) => from + (to - from) * durationProgress(plan, tMs)
	};
}

export function getTransitionPreset(id: string): TransitionPreset {
	const preset = TRANSITION_PRESETS.find((p) => p.id === id);
	if (!preset) {
		throw new TransitionPresetError('unknown_preset', `unknown transition preset: ${id}`);
	}
	return preset;
}

export function planTransitionPreset(preset: TransitionPreset, input: PresetDecksInput): PresetPlan {
	const { outgoing, incoming } = resolvePresetDecks(input, preset.label);
	const outDeck = _deckById(input, outgoing);
	if (!outDeck) {
		throw new TransitionPresetError('no_master_deck', 'outgoing deck missing');
	}

	let window: { start_ms: number; target_ms: number };
	try {
		window = resolveDuration(
			{ unit: 'beats', n: preset.beats, anchor: 'next_beat' },
			{
				position_ms: outDeck.position_ms,
				beatgrid: outDeck.beatgrid,
				phrases: []
			}
		);
	} catch (error) {
		if (error instanceof Error && error.message === 'no_grid') {
			throw new TransitionPresetError('no_grid', 'master deck has no beat grid');
		}
		throw error;
	}

	const gridStartIndex = outDeck.beatgrid.findIndex((b) => b.time_ms === window.start_ms);
	if (gridStartIndex < 0) {
		throw new TransitionPresetError('no_grid', 'could not align preset window to beat grid');
	}

	const endMs = window.target_ms;
	const moves: PresetMovePlan[] = [];

	for (const move of preset.moves) {
		const { role, control } = _parseControlRef(move.control);
		const deckId = role === 'outgoing' ? outgoing : incoming;
		const deck = _deckById(input, deckId);
		if (!deck) {
			throw new TransitionPresetError('needs_two_playing_decks', `deck ${deckId} missing`);
		}
		const beatOffset = move.startBeat ?? 0;
		const beatIndex = gridStartIndex + beatOffset;
		if (beatIndex >= outDeck.beatgrid.length) {
			throw new TransitionPresetError('no_grid', 'startBeat beyond master grid');
		}
		const moveStart = outDeck.beatgrid[beatIndex].time_ms;
		const from = deck.controls[control];
		moves.push(_planMove(deckId, control, from, move.to, moveStart, endMs));
	}

	return {
		presetId: preset.id,
		outgoing,
		incoming,
		clockDeck: outgoing,
		startMs: window.start_ms,
		endMs,
		moves
	};
}

export function presetWriteFor(move: PresetMovePlan, value: number): PresetWrite {
	const v = _clamp01(value);
	if (move.control === 'low' || move.control === 'mid' || move.control === 'high') {
		return { type: 'eq', deck: move.deck, band: move.control, value: v };
	}
	if (move.control === 'filter') {
		return { type: 'filter', deck: move.deck, value: v };
	}
	return { type: 'fader', deck: move.deck, value: v };
}

function _abortRun(active: ActiveRun): void {
	active.aborted = true;
	active.cancelSchedule();
}

function _unregisterRun(presetId: string, active: ActiveRun, outcome: 'completed' | 'aborted'): void {
	_active.delete(presetId);
	active.resolveDone(outcome);
}

export function abortTransitionPreset(id: string): boolean {
	const active = _active.get(id);
	if (!active) return false;
	_abortRun(active);
	_unregisterRun(id, active, 'aborted');
	return true;
}

export function activeTransitionPresetIds(): string[] {
	return [..._active.keys()];
}

export function _resetTransitionPresetsForTests(): void {
	for (const id of [..._active.keys()]) {
		abortTransitionPreset(id);
	}
	_active.clear();
}

function _abortRunsSharingKeys(keys: Set<string>, exceptId: string): void {
	for (const [id, run] of _active) {
		if (id === exceptId) continue;
		if (run.plan.moves.some((m) => keys.has(m.key))) {
			abortTransitionPreset(id);
		}
	}
}

export function runTransitionPreset(
	id: string,
	host: TransitionPresetHost | undefined
): TransitionPresetRunResult {
	const preset = getTransitionPreset(id);

	if (_active.has(id)) {
		abortTransitionPreset(id);
		return { status: 'aborted', presetId: id };
	}

	if (host === undefined) {
		throw new TransitionPresetError('no_host', 'transition preset host required');
	}

	const plan = planTransitionPreset(preset, host.readDecks());
	const moveKeys = new Set(plan.moves.map((m) => m.key));
	_abortRunsSharingKeys(moveKeys, id);

	let resolveDone!: (outcome: 'completed' | 'aborted') => void;
	const done = new Promise<'completed' | 'aborted'>((resolve) => {
		resolveDone = resolve;
	});

	const active: ActiveRun = {
		aborted: false,
		cancelSchedule: () => {},
		plan,
		done,
		resolveDone,
		lastWritten: new Map(),
		host
	};

	const tick = (): void => {
		if (active.aborted) return;

		const input = host.readDecks();
		const clockDeck = input.decks.find((d) => d.id === plan.clockDeck);
		if (!clockDeck?.playing || !clockDeck.loaded) {
			_abortRun(active);
			_unregisterRun(id, active, 'aborted');
			return;
		}

		const pos = clockDeck.position_ms;

		if (pos >= plan.endMs) {
			for (const move of plan.moves) {
				if (active.aborted) return;
				const key = move.key;
				const last = active.lastWritten.get(key);
				if (last !== move.to) {
					host.write(presetWriteFor(move, move.to));
					active.lastWritten.set(key, move.to);
				}
			}
			_unregisterRun(id, active, 'completed');
			return;
		}

		for (const move of plan.moves) {
			if (active.aborted) return;
			if (pos < move.startMs) continue;
			const value = move.valueAt(pos);
			const key = move.key;
			const last = active.lastWritten.get(key);
			if (last === undefined || Math.abs(last - value) > 1e-6) {
				host.write(presetWriteFor(move, value));
				active.lastWritten.set(key, value);
			}
		}

		if (!active.aborted) {
			active.cancelSchedule = host.schedule(tick);
		}
	};

	active.cancelSchedule = host.schedule(tick);
	_active.set(id, active);

	return { status: 'started', plan, done };
}
