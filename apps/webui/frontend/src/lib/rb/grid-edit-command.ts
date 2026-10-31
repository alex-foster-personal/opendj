/**
 * The `grid_edit` performance command's own half (GRIDEDIT-6235/-6235d): the press
 * shape, its validation, and the call into the grid-edit queue.
 *
 * Kept apart from grid-edit.ts (titles, press geometry) and grid-edit-api.ts
 * (HTTP, the anlz cache, the queue) because the performance dispatcher loads
 * on the library page's first paint: this module is all it pulls eagerly, and
 * the request half loads on the first press.
 */

import type { AnlzData } from './deck-state-types';

/** Mirrors `MAX_OFFSET_MS` in apps/shared/state/grid_edit.py (the server refuses past it). */
export const GRID_ADJUST_MAX_OFFSET_MS = 500;

export type GridEditPart = 'adjust' | 'shift';

/** What one press asks the engine to do. */
export type GridEditIntent =
	| { kind: 'nudge'; offset_ms: number; beats: number }
	| { kind: 'reset'; part: GridEditPart };

/** Same micro-ms rounding as the server, so a held run of 2 ms steps stays exact. */
export function roundMs(ms: number): number {
	const rounded = Math.round(ms * 1000) / 1000;
	return rounded === 0 ? 0 : rounded;
}

/** Largest whole-beat step one nudge request may carry (the API's bound). */
export const GRID_SHIFT_MAX_REQUEST_BEATS = 64;

/** Two queued nudges as one request (offsets add, at the server's rounding),
 * or null when they must go as separate steps:
 * - the sum would pass the API's per-request step bound
 *   (+/-GRID_ADJUST_MAX_OFFSET_MS, +/-64 beats), which the backend refuses whole;
 * - the offsets move in opposite directions: the server lands a step past the
 *   stored limit AT the limit, so near it the order matters (from +490 ms,
 *   +20 then -10 ends at +490, their sum +10 at +500). */
export function mergeNudges(
	a: { offset_ms: number; beats: number },
	b: { offset_ms: number; beats: number }
): { offset_ms: number; beats: number } | null {
	if (a.offset_ms * b.offset_ms < 0) return null;
	const merged = { offset_ms: roundMs(a.offset_ms + b.offset_ms), beats: a.beats + b.beats };
	if (Math.abs(merged.offset_ms) > GRID_ADJUST_MAX_OFFSET_MS) return null;
	if (Math.abs(merged.beats) > GRID_SHIFT_MAX_REQUEST_BEATS) return null;
	return merged;
}

/** Largest whole-beat step one press may ask for: four beats is the same bar 1. */
const _MAX_STEP_BEATS = 3;

/**
 * Validates a `grid_edit` performance command's `edit` field, which arrives
 * over the untyped IPC bridge from an agent as well as from the deck buttons.
 * A nudge is one press (or presses merged by the caller): a finite ms step no
 * larger than the stored range and a whole-beat step under a bar.
 */
export function parseGridEditIntent(value: unknown): GridEditIntent {
	if (typeof value !== 'object' || value === null || Array.isArray(value)) {
		throw new TypeError('grid_edit edit must be an object');
	}
	const record = value as Record<string, unknown>;
	const keys = Object.keys(record).sort().join(',');
	if (record.kind === 'nudge') {
		if (keys !== 'beats,kind,offset_ms') throw new TypeError(`grid_edit nudge takes kind, offset_ms, beats; got ${keys}`);
		const { offset_ms, beats } = record;
		if (typeof offset_ms !== 'number' || !Number.isFinite(offset_ms) || Math.abs(offset_ms) > GRID_ADJUST_MAX_OFFSET_MS) {
			throw new RangeError(`grid_edit offset_ms must be a finite number within +/-${GRID_ADJUST_MAX_OFFSET_MS}; got ${String(offset_ms)}`);
		}
		if (typeof beats !== 'number' || !Number.isInteger(beats) || Math.abs(beats) > _MAX_STEP_BEATS) {
			throw new RangeError(`grid_edit beats must be a whole number within +/-${_MAX_STEP_BEATS}; got ${String(beats)}`);
		}
		return { kind: 'nudge', offset_ms: roundMs(offset_ms), beats };
	}
	if (record.kind === 'reset') {
		if (keys !== 'kind,part') throw new TypeError(`grid_edit reset takes kind, part; got ${keys}`);
		if (record.part !== 'adjust' && record.part !== 'shift') {
			throw new TypeError(`grid_edit reset part must be "adjust" or "shift"; got ${String(record.part)}`);
		}
		return { kind: 'reset', part: record.part };
	}
	throw new TypeError(`grid_edit edit kind must be "nudge" or "reset"; got ${String(record.kind)}`);
}

/** The command body: applies one press to the grid of the track a deck holds. */
export async function runGridEditCommand(
	stableId: string | null,
	anlz: AnlzData | null,
	intent: GridEditIntent,
	guard?: () => void
): Promise<void> {
	const { runGridEdit } = await import('./grid-edit-api');
	await runGridEdit(stableId, anlz, intent, guard);
}

/*
 * GRIDEDIT-6235: a held grid-adjust button repeats every 80 ms, and every
 * `grid_edit` waits on its deck's persistence scope while the one ahead writes
 * and re-reads the grid. Without this, each repeat would queue as its own
 * command, so a slow backend writes one nudge per tick and keeps writing a
 * backlog after the button is let go (Codex P2, PR #6235).
 *
 * A nudge dispatched while the deck's last-queued `grid_edit` is a nudge that
 * has not started yet, for the same track and grid, merges into that command
 * and shares its result. Anything else scheduled on the deck's persistence
 * scope after it (a reset, a hot-cue write, a preset or rescue barrier) ends
 * the merge window, so no press ever jumps ahead of a command queued before it.
 */
interface _Nudge {
	deck: number;
	edit: GridEditIntent;
}

export interface GridNudgeCoalescer<C extends _Nudge, T> {
	/** Merge `command` into the deck's queued nudge; its result, or null to queue normally. */
	merge(command: C, sameTarget: (queued: C) => boolean): Promise<T> | null;
	/** `command` was just scheduled; a later nudge may merge into it until it starts. */
	queued(command: C, settled: Promise<T>): void;
	/** `command` started running; nothing may merge into it any more. */
	started(command: C): void;
	/** Something was scheduled on these scopes: close their decks' merge windows. */
	cut(scopes: readonly PropertyKey[]): void;
	clear(): void;
}

export function createGridNudgeCoalescer<C extends _Nudge, T>(
	persistenceDeck: (scope: PropertyKey) => number | null
): GridNudgeCoalescer<C, T> {
	const pending = new Map<number, { command: C; settled: Promise<T> }>();
	return {
		merge(command, sameTarget) {
			if (command.edit.kind !== 'nudge') return null;
			const queued = pending.get(command.deck);
			if (queued === undefined || queued.command.edit.kind !== 'nudge' || !sameTarget(queued.command)) return null;
			const merged = mergeNudges(queued.command.edit, command.edit);
			if (merged === null) return null;
			queued.command.edit = { kind: 'nudge', ...merged };
			return queued.settled;
		},
		queued(command, settled) {
			if (command.edit.kind === 'nudge') pending.set(command.deck, { command, settled });
		},
		started(command) {
			if (pending.get(command.deck)?.command === command) pending.delete(command.deck);
		},
		cut(scopes) {
			for (const scope of scopes) {
				const deck = persistenceDeck(scope);
				if (deck !== null) pending.delete(deck);
			}
		},
		clear() {
			pending.clear();
		}
	};
}
