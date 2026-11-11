import type { PerformanceUiCommandResult } from '$lib/rb/performance-ipc-types';

/** Named refusal text for panel display when the dispatcher answers without a session. */
export function refusalMessageFromUiResult(
	result: Extract<PerformanceUiCommandResult, { ok: false }>
): string {
	const error = result.error;
	if (error instanceof Error) return error.message;
	return String(error);
}

export type OutputRefreshHooks = {
	setPending: (pending: boolean) => void;
	setLocalRefusal: (message: string | null) => void;
};

/** Await output refresh; pending always clears in `finally`; local refusal only for `no_session`. */
export async function runOutputRefresh(
	onrefresh: () => Promise<PerformanceUiCommandResult>,
	hooks: OutputRefreshHooks
): Promise<void> {
	hooks.setPending(true);
	try {
		const result = await onrefresh();
		if (result.ok) {
			hooks.setLocalRefusal(null);
		} else if (result.reason === 'no_session') {
			hooks.setLocalRefusal(refusalMessageFromUiResult(result));
		}
	} finally {
		hooks.setPending(false);
	}
}
