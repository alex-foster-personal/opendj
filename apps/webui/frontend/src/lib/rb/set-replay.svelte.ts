/**
 * SETREC-12067 skeleton replay player (issue #10736). SETREC-11 (#10735) will read
 * set bundles, apply initial_state.json, and schedule controls.jsonl here.
 * Take-over is page-side: taken-over controls never receive replay dispatches, so
 * the engine only sees live movers (User / Controller / agent), not replay source
 * on the wire (that is SETREC-11's engine work).
 */
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import type { PerformanceCommand } from './performance-ipc-types';
import {
	beginReplayTakeoverSession,
	endReplayTakeoverSession,
	isReplayControlLive,
	replayTakeover,
	takeOverAllReplay,
	takeOverReplayControl,
	type ReplayEventSource
} from './set-replay-takeover.svelte';
import { isReplayControlId, replayControlId, REPLAY_CONTROL_IDS } from './set-replay-control-ids';

export function startReplay(): void {
	beginReplayTakeoverSession();
}

export function stopReplay(): void {
	endReplayTakeoverSession();
}

/** Dispatch one recorded control change as source=replay. */
export async function dispatchReplayEvent(
	command: PerformanceCommand
): Promise<'dispatched' | 'taken_over' | 'inactive'> {
	if (!replayTakeover.active) return 'inactive';
	const id = replayControlId(command);
	if (replayTakeover.taken_over_all || (id !== null && isReplayControlLive(id))) {
		return 'taken_over';
	}
	await dispatchPerformanceCommand(command, undefined, 'replay');
	return 'dispatched';
}

function formatKnownIds(): string {
	const sample = REPLAY_CONTROL_IDS.slice(0, 4).join(', ');
	return `expected all or one of ${sample}, ...`;
}

/** Page side of opendj replay takeover / MCP replay_takeover / ReplayTakeoverBar. */
export function replayTakeOver(
	target: string,
	source: ReplayEventSource
): { taken_over: string | 'all'; live: string[] } {
	if (!replayTakeover.active) {
		throw new Error('no replay is running; nothing to take over');
	}
	if (target === 'all') {
		takeOverAllReplay(source);
		return { taken_over: 'all', live: Object.keys(replayTakeover.live) };
	}
	if (!isReplayControlId(target)) {
		throw new Error(`unknown control id ${target}; ${formatKnownIds()}`);
	}
	takeOverReplayControl(target, source);
	return { taken_over: target, live: Object.keys(replayTakeover.live) };
}
