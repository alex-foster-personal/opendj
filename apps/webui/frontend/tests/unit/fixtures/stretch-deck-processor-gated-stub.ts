/**
 * StretchDeckProcessor stand-in that records every schedule() and can hold
 * the next one in flight, so a test can queue a second schedule behind it.
 * Same shape as stretch-deck-processor-load-stub.ts otherwise. The engine
 * bundle gets its own copy of this module, so the test reaches the recorder
 * through `globalThis.__gatedStretchStub`.
 */
export const scheduleCalls: Array<{ outputTime: number; at: number }> = [];
let held: Promise<void> | null = null;

/** Holds the next schedule() until the returned function is called. */
export function holdNextSchedule(): () => void {
	let release!: () => void;
	held = new Promise<void>((resolve) => {
		release = resolve;
	});
	return release;
}

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
			async schedule(outputTime: number, _change: unknown) {
				scheduleCalls.push({ outputTime, at: performance.now() });
				const gate = held;
				held = null;
				if (gate !== null) await gate;
			},
			async stop(_outputTime: number) {},
			async dispose() {}
		};
	}
}

export async function ensureStretchWorkletReady(_context: AudioContext) {
	return async () => {
		throw new Error('stretch-deck-processor-gated-stub must not create worklet nodes');
	};
}

export function resetStretchWorkletReadyForTests() {}
export function setStretchAddModuleTimeoutMs(_ms: number) {}
export function resetStretchAddModuleTimeoutMs() {}
export function injectStretchAddModuleFailures(_count: number) {}
export function pendingInjectedAddModuleFailures() {
	return 0;
}

(globalThis as { __gatedStretchStub?: unknown }).__gatedStretchStub = { scheduleCalls, holdNextSchedule };
