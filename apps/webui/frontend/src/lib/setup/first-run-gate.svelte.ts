/**
 * Reactive state for the root layout's first-run auto-open (issue #2722).
 *
 * resolveFirstRun() can take several probe cycles on a cold engine; failures
 * must never be silent. This store holds resolving / error / retry for the
 * boot screen and layout to render.
 */
import { resolveFirstRun, FirstRunTimeoutError } from './first-run';

export type FirstRunGatePhase = 'idle' | 'resolving' | 'error';

let phase = $state<FirstRunGatePhase>('idle');
let error = $state<string | null>(null);
let attempt = $state(0);

function _message(exc: unknown): string {
	return exc instanceof Error ? exc.message : String(exc);
}

/** Whether the setup wizard should open, or null when still resolving. */
export async function runFirstRunGate(
	options: { timeoutMs?: number } = {}
): Promise<boolean | null> {
	phase = 'resolving';
	error = null;
	attempt += 1;
	try {
		const show = await resolveFirstRun(options);
		phase = 'idle';
		return show;
	} catch (exc) {
		phase = 'error';
		error =
			exc instanceof FirstRunTimeoutError
				? exc.message
				: `Could not check first-run setup: ${_message(exc)}`;
		return null;
	}
}

export function retryFirstRunGate(
	options: { timeoutMs?: number } = {}
): Promise<boolean | null> {
	return runFirstRunGate(options);
}

export function _resetFirstRunGateForTests(): void {
	phase = 'idle';
	error = null;
	attempt = 0;
}

export const firstRunGate = {
	get phase() {
		return phase;
	},
	get error() {
		return error;
	},
	get attempt() {
		return attempt;
	},
	get isResolving() {
		return phase === 'resolving';
	},
	get hasError() {
		return phase === 'error' && error !== null;
	}
};
