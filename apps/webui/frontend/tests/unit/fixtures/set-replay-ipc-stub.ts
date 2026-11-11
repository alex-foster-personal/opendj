import type { PerformanceCommand } from '$lib/rb/performance-ipc-types';
import type { CommandSource } from '$lib/rb/deck-stop-log';
import { replayDispatchGate } from '$lib/rb/set-replay-takeover.svelte';
import { replayControlId } from '$lib/rb/set-replay-control-ids';
import type { PerformanceState } from '$lib/rb/performance-ipc-types';

export const dispatchRecords: Array<{ type: string; control: string | null; source: CommandSource }> =
	[];

export async function dispatchPerformanceCommand(
	command: PerformanceCommand,
	_pressT0Ms?: number,
	source: CommandSource = 'app-command'
): Promise<PerformanceState> {
	if (replayDispatchGate(command, source) === 'drop') {
		return {} as PerformanceState;
	}
	dispatchRecords.push({
		type: command.type,
		control: replayControlId(command),
		source
	});
	return {} as PerformanceState;
}

export function queryPerformanceState(): PerformanceState {
	return {} as PerformanceState;
}

export function installPerformanceHotCueDriver(): () => void {
	return () => {};
}

export function runSyncScopedWork<T>(work: () => T): T {
	return work();
}
