/**
 * RBPERF (#11143, parent #285): command stamp to first presented `position_ms`
 * at the engine's quantized seek target. Presentation clock stays transport
 * truth; this module only observes it.
 */

import type { DeckId } from '$lib/rb/deck-slots';
import { recordPerfTiming } from '$lib/rb/perf-event-log';
import { measurePressToScheduleMs } from '$lib/rb/press-stamp';

export const SEEK_PRESENTED_KIND = 'seek-presented';

/** Maximum delta between presented and quantized target to count as landed. */
const TARGET_MATCH_MS = 0.5;

type PendingSeekPresented = {
	readonly targetMs: number;
	readonly pressT0Ms: number;
};

const _pending = new Map<DeckId, PendingSeekPresented>();
const _pressMarks = new Map<DeckId, number>();

/** Q1 stamp queued by the dispatcher before `quantizedSeek` computes its target. */
export function markSeekPresentedPress(deck: DeckId, pressT0Ms: number): void {
	if (measurePressToScheduleMs(pressT0Ms, deck) === undefined) return;
	_pressMarks.set(deck, pressT0Ms);
}

export function armSeekPresentedFromPressMark(deck: DeckId, targetMs: number): void {
	const pressT0Ms = _pressMarks.get(deck);
	if (pressT0Ms === undefined) return;
	_pressMarks.delete(deck);
	armSeekPresented(deck, targetMs, pressT0Ms);
}

export function presentedMatchesSeekTarget(presentedMs: number, targetMs: number): boolean {
	if (!Number.isFinite(presentedMs) || !Number.isFinite(targetMs)) return false;
	return Math.abs(presentedMs - targetMs) <= TARGET_MATCH_MS;
}

/**
 * Begin timing a seek once the engine knows the quantized landing ms.
 * Replaces any earlier pending row on this deck without completing it.
 */
export function armSeekPresented(deck: DeckId, targetMs: number, pressT0Ms: number): void {
	if (!Number.isFinite(targetMs) || targetMs < 0) return;
	if (measurePressToScheduleMs(pressT0Ms, deck) === undefined) return;
	_pending.set(deck, { targetMs, pressT0Ms });
}

/** Drop a pending measurement without recording (deck unload, superseded arm). */
export function cancelSeekPresented(deck: DeckId): void {
	_pending.delete(deck);
}

/** Test whether a deck still waits for its target to present. */
export function seekPresentedPending(deck: DeckId): boolean {
	return _pending.has(deck);
}

/**
 * Called when published transport truth reports a new `position_ms`.
 * Completes at most one row per armed seek.
 */
export function noteSeekPresentedPosition(deck: DeckId, presentedMs: number): void {
	const pending = _pending.get(deck);
	if (pending === undefined) return;
	if (!presentedMatchesSeekTarget(presentedMs, pending.targetMs)) return;
	const commandToPresentedMs = performance.now() - pending.pressT0Ms;
	if (!Number.isFinite(commandToPresentedMs) || commandToPresentedMs < 0) {
		_pending.delete(deck);
		return;
	}
	_pending.delete(deck);
	recordPerfTiming(
		SEEK_PRESENTED_KIND,
		{ command_to_presented_ms: Math.round(commandToPresentedMs * 1000) / 1000 },
		deck,
		{ target_ms: String(Math.round(pending.targetMs)) }
	);
}
