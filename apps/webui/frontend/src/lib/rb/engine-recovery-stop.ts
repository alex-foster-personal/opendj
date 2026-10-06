/**
 * Bug #58 (Tue 6 Oct 2026): a stop the ENGINE caused while recovering its audio
 * graph is not the operator stopping the set.
 *
 * The soak unload went: output stall, graph rebuild, Signalsmith addModule timed
 * out, both decks unloaded (IOPIN-12), then 31 s later AutoPlay's idle disarm
 * read "nothing is playing" as "the set is over" and switched itself off. So the
 * set could not resume on its own even once the engine was healthy again.
 *
 * The rebuild path tags the stop with a reason here. While a tag is set,
 * AutoPlay's idle disarm and its silent-idle stall stand down: AutoPlay stays
 * enabled and armed, and carries on as soon as a deck plays again, which is the
 * moment the tag is cleared.
 */

export type EngineRecoveryStopReason = 'graph-rebuild' | 'graph-rebuild-failed';

let _reason: EngineRecoveryStopReason | null = null;

export function markEngineRecoveryStop(reason: EngineRecoveryStopReason): void {
	_reason = reason;
}

export function clearEngineRecoveryStop(): void {
	_reason = null;
}

export function engineRecoveryStopReason(): EngineRecoveryStopReason | null {
	return _reason;
}
