/**
 * Orchestrate output-stall recovery: suspend/resume first, then recreate the
 * graph with loaded decks re-attached. Pure effects injection; no AudioContext
 * subclassing (issue #2155).
 */
import { REBIND_COOLDOWN_MS, type OutputRebindHandle } from '$lib/rb/audio-output-rebind';

export interface OutputStallRecoveryEffects {
	recreateGraph: (() => Promise<void>) | null;
	pushToast(message: string, kind: 'info' | 'error'): void;
	recordPerfEvent(kind: string, message: string, severity: 'info' | 'error'): void;
	now(): number;
}

export interface OutputStallRecoveryHandle {
	recover(): Promise<void>;
}

export function installOutputStallRecovery(
	rebind: OutputRebindHandle,
	effects: OutputStallRecoveryEffects
): OutputStallRecoveryHandle {
	let lastRecoverAt = Number.NEGATIVE_INFINITY;
	let recovering = false;

	async function recover(): Promise<void> {
		if (effects.recreateGraph === null) return;
		if (recovering) return;
		if (effects.now() - lastRecoverAt < REBIND_COOLDOWN_MS) return;
		recovering = true;
		try {
			await rebind.request('output position stalled');
			try {
				await effects.recreateGraph();
				effects.recordPerfEvent(
					'audio-output-recreated',
					'audio graph recreated after a frozen device output position',
					'info'
				);
			} catch (error: unknown) {
				const message = error instanceof Error ? error.message : String(error);
				effects.recordPerfEvent(
					'audio-output-recreate-failed',
					`audio graph recreate failed after output position stall: ${message}`,
					'error'
				);
				effects.pushToast(
					'NO AUDIO OUTPUT: graph recreate failed. Reload the page (Cmd+R).',
					'error'
				);
			}
		} finally {
			lastRecoverAt = effects.now();
			recovering = false;
		}
	}

	return { recover };
}
