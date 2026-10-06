/**
 * Minimal StretchDeckProcessor stand-in for deck-load unit tests.
 *
 * The real adapter pulls in Signalsmith worklets, which node:test cannot host.
 * These tests only need the load() critical path to decode audio and publish
 * deck state after a 200 /anlz rescue.
 */
export class StretchDeckProcessor {
	static async create(_context: AudioContext, _options: { onProcessorError: (error: unknown) => void }) {
		return {
			connect(_destination: AudioNode) {},
			disconnect() {},
			async load(_buffer: AudioBuffer, _requested: 'copy' | 'transfer' = 'copy') {
				return 'copy' as const;
			},
			async latencySec() {
				return 0.12;
			},
			async schedule(_outputTime: number, _change: unknown) {},
			async stop(_outputTime: number) {},
			async dispose() {}
		};
	}
}

export async function ensureStretchWorkletReady(_context: AudioContext) {
	return async () => {
		throw new Error('stretch-deck-processor-load-stub must not create worklet nodes');
	};
}

export function resetStretchWorkletReadyForTests() {}

// Bug #58: the rebuild retry sets the addModule timeout and the fault drill injects failures.
export const stubAddModuleTimeouts: number[] = [];
export function setStretchAddModuleTimeoutMs(ms: number) {
	stubAddModuleTimeouts.push(ms);
}
export function resetStretchAddModuleTimeoutMs() {}
let stubInjectedFailures = 0;
export function injectStretchAddModuleFailures(count: number) {
	stubInjectedFailures = count;
}
export function pendingInjectedAddModuleFailures() {
	return stubInjectedFailures;
}
