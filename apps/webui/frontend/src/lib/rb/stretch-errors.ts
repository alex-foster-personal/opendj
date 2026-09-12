/** Shared stretch error types and command timeout helper (leaf for worklet cycle break). */

export const STRETCH_CREATE_TIMEOUT_MS = 15_000;
export const STRETCH_COMMAND_TIMEOUT_MS = 5_000;

export class StretchCommandTimeoutError extends Error {
	constructor(operation: string, timeoutMs: number) {
		super(`Signalsmith ${operation} timed out after ${timeoutMs}ms`);
		this.name = 'StretchCommandTimeoutError';
	}
}

export class StretchProcessorError extends Error {
	constructor(message: string, options?: ErrorOptions) {
		super(message, options);
		this.name = 'StretchProcessorError';
	}
}

export async function withStretchCommandTimeout<T>(
	command: Promise<T>,
	operation: string,
	timeoutMs = STRETCH_COMMAND_TIMEOUT_MS
): Promise<T> {
	if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
		throw new RangeError(`timeoutMs must be a finite positive number, got ${timeoutMs}`);
	}
	let timer: ReturnType<typeof setTimeout> | undefined;
	try {
		return await Promise.race([
			command,
			new Promise<never>((_resolve, reject) => {
				timer = setTimeout(
					() => reject(new StretchCommandTimeoutError(operation, timeoutMs)),
					timeoutMs
				);
			})
		]);
	} finally {
		if (timer !== undefined) clearTimeout(timer);
	}
}
