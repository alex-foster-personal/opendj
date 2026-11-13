/**
 * Bounded waits for AudioContext IO that can hang on a clockless default output.
 *
 * Issue #2150: BlackHole 2ch with no consumer never starts IO, so `resume()` and
 * `suspend()` can stay unresolved forever. Every await on those calls must stay
 * inside the output watchdog window and fail loud rather than freezing play.
 */

export const AUDIO_CONTEXT_IO_TIMEOUT_MS = 2_500;

export const AUDIO_OUTPUT_DEAD_TOAST =
	'NO AUDIO OUTPUT: the default output device did not start within the watchdog window.';

export class AudioContextIoTimeoutError extends Error {
	readonly operation: string;
	readonly timeoutMs: number;

	constructor(operation: string, timeoutMs: number) {
		super(`AudioContext ${operation} timed out after ${timeoutMs}ms`);
		this.name = 'AudioContextIoTimeoutError';
		this.operation = operation;
		this.timeoutMs = timeoutMs;
	}
}

export interface AudioContextIoTimeoutScheduler {
	setTimeout(fn: () => void, ms: number): unknown;
	clearTimeout(handle: unknown): void;
}

const defaultIoTimeoutScheduler: AudioContextIoTimeoutScheduler = {
	setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
	clearTimeout: (handle) =>
		globalThis.clearTimeout(handle as unknown as ReturnType<typeof globalThis.setTimeout>)
};

export async function withAudioContextIoTimeout<T>(
	operation: string,
	promise: Promise<T>,
	timeoutMs: number = AUDIO_CONTEXT_IO_TIMEOUT_MS,
	scheduler: AudioContextIoTimeoutScheduler = defaultIoTimeoutScheduler
): Promise<T> {
	if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
		throw new RangeError(`timeoutMs must be a finite positive number, got ${timeoutMs}`);
	}
	let timer: unknown;
	try {
		return await Promise.race([
			promise,
			new Promise<never>((_resolve, reject) => {
				timer = scheduler.setTimeout(
					() => reject(new AudioContextIoTimeoutError(operation, timeoutMs)),
					timeoutMs
				);
			})
		]);
	} finally {
		if (timer !== undefined) scheduler.clearTimeout(timer);
	}
}
