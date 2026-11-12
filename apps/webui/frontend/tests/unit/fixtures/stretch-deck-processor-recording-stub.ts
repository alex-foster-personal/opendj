/**
 * Recording StretchDeckProcessor stand-in for deck-stems-held-release.test.mjs.
 *
 * The real adapter wraps the Signalsmith AudioWorklet, which node:test cannot
 * host. This stand-in keeps the adapter's surface and records what the engine
 * did to each processor it created, in creation order, on
 * `globalThis.__stretchStubProcessors`: whether it is connected, whether it was
 * disposed, and every schedule it was sent. A test can then tell a real
 * processor swap (the mix processor retired, the stem branches scheduled) from
 * a deck state that merely says `ready`, and can fire a processor's own error
 * callback the way a failing worklet would.
 *
 * Creation order for one stemmed load: the mix processor first, then one
 * processor per stem part.
 */
export interface RecordedStretchProcessor {
	/** The AudioContext the engine created this processor in; real-engine-rig.mjs scopes `processors()` to its own contexts with it. */
	context: unknown;
	options: { onProcessorError: (error: unknown) => void };
	connected: boolean;
	disposed: boolean;
	schedules: Array<{ outputTime: number; change: Record<string, unknown> }>;
	/** Incremented at the top of each `schedule()` call; tests can wait on entry. */
	scheduleEntered: number;
	/** When set, the next `schedule()` clears it and rejects with it (one-shot). */
	scheduleError?: Error;
	/** When set, `schedule()` awaits this before it records the change. */
	scheduleHold?: Promise<void>;
	/** Set by a test before the swap to make `connect` throw: a swap that fails on a deck that is not playing. */
	connectError?: Error;
	/** Awaited by `dispose` first, so a test can hold the window a failed swap leaves open. */
	beforeDispose?: () => Promise<void>;
}

function registry(): RecordedStretchProcessor[] {
	const scope = globalThis as typeof globalThis & {
		__stretchStubProcessors?: RecordedStretchProcessor[];
	};
	scope.__stretchStubProcessors ??= [];
	return scope.__stretchStubProcessors;
}

export class StretchDeckProcessor {
	static async create(
		context: AudioContext,
		options: { onProcessorError: (error: unknown) => void }
	) {
		const record: RecordedStretchProcessor = {
			context,
			options,
			connected: false,
			disposed: false,
			schedules: [],
			scheduleEntered: 0
		};
		registry().push(record);
		return {
			connect(_destination: AudioNode) {
				if (record.disposed) throw new Error('stretch stub: connect after dispose');
				if (record.connectError !== undefined) throw record.connectError;
				record.connected = true;
			},
			disconnect() {
				record.connected = false;
			},
			async load(_buffer: AudioBuffer) {},
			async latencySec() {
				return 0.12;
			},
			async schedule(outputTime: number, change: Record<string, unknown>) {
				if (record.disposed) throw new Error('stretch stub: schedule after dispose');
				record.scheduleEntered += 1;
				if (record.scheduleError !== undefined) {
					const err = record.scheduleError;
					delete record.scheduleError;
					throw err;
				}
				if (record.scheduleHold !== undefined) await record.scheduleHold;
				record.schedules.push({ outputTime, change });
			},
			async stop(outputTime: number) {
				record.schedules.push({ outputTime, change: { active: false } });
			},
			async dispose() {
				if (record.beforeDispose !== undefined) await record.beforeDispose();
				record.disposed = true;
				record.connected = false;
			}
		};
	}
}

export async function ensureStretchWorkletReady(_context: AudioContext) {
	return async () => {
		throw new Error('stretch-deck-processor-recording-stub must not create worklet nodes');
	};
}

export function resetStretchWorkletReadyForTests() {}
export function setStretchAddModuleTimeoutMs(_ms: number) {}
export function resetStretchAddModuleTimeoutMs() {}
export function injectStretchAddModuleFailures(_count: number) {}
