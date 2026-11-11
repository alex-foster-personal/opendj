/**
 * Shared async click runner: one pending key at a time, error toast on reject.
 * Parent audit #3184; first slice wired from issue #11281.
 */
import { pushToastForFailure } from '$lib/stores.svelte';

export type RunButtonActionGate = {
	read: () => string | null;
	write: (key: string | null) => void;
};

export function runButtonActionGate(
	read: () => string | null,
	write: (key: string | null) => void
): RunButtonActionGate {
	return { read, write };
}

/** True when any action is in flight, or only the named key. */
export function isRunButtonActionPending(gate: RunButtonActionGate, key?: string): boolean {
	const pending = gate.read();
	if (pending === null) return false;
	return key === undefined ? true : pending === key;
}

export type RunButtonActionOptions = {
	/** When set, passed to pushToast instead of the thrown value's message. */
	toastMessage?: (exc: unknown) => string;
};

/**
 * Run one button action: refuse overlap, hold pending for the work, toast failures.
 * Returns whether the work finished without throwing.
 */
export async function runButtonAction(
	gate: RunButtonActionGate,
	key: string,
	work: () => void | Promise<void>,
	options?: RunButtonActionOptions
): Promise<boolean> {
	if (gate.read() !== null) return false;
	gate.write(key);
	try {
		await work();
		return true;
	} catch (exc) {
		const message = options?.toastMessage?.(exc);
		pushToastForFailure(exc, message);
		return false;
	} finally {
		if (gate.read() === key) gate.write(null);
	}
}
