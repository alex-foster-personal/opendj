/**
 * SETREC-12067: replay take-over registry and dispatch gate (page-side only).
 * SETREC-11 (#10735) adds bundle playback; this module only gates dispatch.
 */
import type { PerformanceCommand } from './performance-ipc-types';
import type { CommandSource } from './deck-stop-log';
import { isReplayControlId, replayControlId } from './set-replay-control-ids';

export type ReplayEventSource = 'user' | 'controller' | 'agent' | 'replay';

export interface ReplayLiveControl {
	control: string;
	source: ReplayEventSource;
	at_ms: number;
}

export const replayTakeover = $state({
	active: false,
	taken_over_all: false,
	live: {} as Record<string, ReplayLiveControl>,
	dropped: 0
});

export function v2EventSource(source: CommandSource): ReplayEventSource {
	switch (source) {
		case 'user-ui':
			return 'user';
		case 'agent-command':
			return 'agent';
		case 'replay':
			return 'replay';
		default:
			return 'user';
	}
}

export function beginReplayTakeoverSession(): void {
	replayTakeover.active = true;
	replayTakeover.taken_over_all = false;
	replayTakeover.live = {};
	replayTakeover.dropped = 0;
}

export function endReplayTakeoverSession(): void {
	replayTakeover.active = false;
	replayTakeover.taken_over_all = false;
	replayTakeover.live = {};
}

export function isReplayControlLive(control: string): boolean {
	return replayTakeover.taken_over_all || control in replayTakeover.live;
}

export function takeOverReplayControl(control: string, source: ReplayEventSource): void {
	if (control in replayTakeover.live) return;
	const at_ms = performance.now();
	replayTakeover.live = {
		...replayTakeover.live,
		[control]: { control, source, at_ms }
	};
}

export function takeOverAllReplay(source: ReplayEventSource): void {
	replayTakeover.taken_over_all = true;
	if (Object.keys(replayTakeover.live).length === 0) {
		replayTakeover.live = {
			all: { control: 'all', source, at_ms: performance.now() }
		};
	}
}

/** Synchronous gate on dispatchPerformanceCommand entry. */
export function replayDispatchGate(
	command: PerformanceCommand,
	source: CommandSource
): 'dispatch' | 'drop' {
	const controlId = replayControlId(command);
	if (source === 'replay') {
		if (!replayTakeover.active || replayTakeover.taken_over_all) {
			replayTakeover.dropped += 1;
			return 'drop';
		}
		if (controlId !== null && isReplayControlLive(controlId)) {
			replayTakeover.dropped += 1;
			return 'drop';
		}
		return 'dispatch';
	}
	if (replayTakeover.active && controlId !== null && !(controlId in replayTakeover.live)) {
		const mapped = v2EventSource(source);
		replayTakeover.live = {
			...replayTakeover.live,
			[controlId]: { control: controlId, source: mapped, at_ms: performance.now() }
		};
	}
	return 'dispatch';
}

/** Execute-time re-check for replay commands queued before a live touch. */
export function replayDispatchTakenOver(
	command: PerformanceCommand,
	source: CommandSource
): boolean {
	if (source !== 'replay') return false;
	const controlId = replayControlId(command);
	if (!replayTakeover.active || replayTakeover.taken_over_all) return true;
	if (controlId !== null && isReplayControlLive(controlId)) return true;
	return false;
}
