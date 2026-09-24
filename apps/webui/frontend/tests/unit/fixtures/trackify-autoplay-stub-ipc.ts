/**
 * Stub performance-ipc for Trackify autoplay controller tests.
 */

export type DispatchCmd = {
	type: string;
	deck?: number;
	stable_id?: string;
	playing?: boolean;
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
