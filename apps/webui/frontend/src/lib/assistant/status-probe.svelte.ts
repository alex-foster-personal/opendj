import { getAssistantStatus, type AssistantStatus } from './assistant-api';

function messageOf(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

/** Probe GET /api/v1/assistant/status whenever `isOpen()` becomes true. */
export function createAssistantStatusProbe(isOpen: () => boolean) {
	let status = $state.raw<AssistantStatus | null>(null);
	let statusError = $state<string | null>(null);

	$effect(() => {
		if (!isOpen()) return;
		statusError = null;
		const probe = new AbortController();
		getAssistantStatus(probe.signal).then(
			(value) => (status = value),
			(error: unknown) => {
				if (!probe.signal.aborted) statusError = messageOf(error);
			}
		);
		return () => probe.abort();
	});

	return {
		get status() {
			return status;
		},
		get statusError() {
			return statusError;
		}
	};
}
