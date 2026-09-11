/**
 * Typed handoff failure that preserves the point at which AutoPlay stopped.
 * The controller uses it to decide whether retrying can duplicate a load.
 */
import type { AutoPlayHandoffPhase } from '$lib/rb/auto-play';

export class AutoPlayHandoffError extends Error {
	readonly phase: AutoPlayHandoffPhase;

	constructor(phase: AutoPlayHandoffPhase, cause: unknown) {
		super(cause instanceof Error ? cause.message : String(cause), { cause });
		this.name = 'AutoPlayHandoffError';
		this.phase = phase;
	}
}
