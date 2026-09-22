/**
 * Stub performance-ipc for AutoPlay Next orchestrator tests.
 */

export type DispatchCmd = {
	type: string;
	deck?: number;
	loop?: { in_ms: number; out_ms: number } | null;
	beats?: number;
	start_ms?: number;
	band?: string;
	value?: number;
	[key: string]: unknown;
};

const _calls: DispatchCmd[] = [];
let _impl: ((cmd: DispatchCmd) => Promise<void>) | null = null;

export function setDispatchPerformanceCommandStub(
	fn: ((cmd: DispatchCmd) => Promise<void>) | null
): void {
	_impl = fn;
}

export function getDispatchPerformanceCommandCalls(): readonly DispatchCmd[] {
	return _calls;
}

export function resetDispatchPerformanceCommandStub(): void {
	_calls.length = 0;
	_impl = null;
}

export async function dispatchPerformanceCommand(cmd: DispatchCmd): Promise<unknown> {
	_calls.push({ ...cmd });
	if (_impl !== null) {
		return _impl(cmd);
	}
	return undefined;
}
