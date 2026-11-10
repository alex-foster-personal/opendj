/** Test stub for `$lib/assistant/status-probe.svelte`: SSR runs no $effect, so
 * the real probe never answers. Tests set `fakeAssistantProbe` before render. */
export const fakeAssistantProbe = { status: null, statusError: null };

export function createAssistantStatusProbe() {
	return {
		get status() {
			return fakeAssistantProbe.status;
		},
		get statusError() {
			return fakeAssistantProbe.statusError;
		}
	};
}
