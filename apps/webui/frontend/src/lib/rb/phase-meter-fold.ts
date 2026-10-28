/**
 * TOPVIEW-02/03: the tempo fold the phase views read a follower at, from the
 * engine that is playing. Web Audio keeps its locks in the engine's phase
 * lock; Rust mode keeps them in `rust-phase-lock.ts` (Sol P1 4301959423).
 */
import type { TempoNormalization } from '$lib/rb/beat-sync-math';

/** The active engine's Beat Sync lock fold for a deck, or null when it holds
 * no lock. An exact-tempo lock is 1, not null: the Phase scope reads a locked
 * deck against the master whatever the fold (Sol P1 4349021851). */
export function phaseLockFold(
	rustEnabled: boolean,
	rustNormalization: TempoNormalization | undefined,
	webAudioFold: () => TempoNormalization | null
): TempoNormalization | null {
	return rustEnabled ? (rustNormalization ?? null) : webAudioFold();
}

/** The fold the Phase meter reads at: the lock's, or 1 with no lock, since
 * the meter measures beat-level offset either way. */
export function phaseMeterFold(
	rustEnabled: boolean,
	rustNormalization: TempoNormalization | undefined,
	webAudioFold: () => TempoNormalization | null
): TempoNormalization {
	return phaseLockFold(rustEnabled, rustNormalization, webAudioFold) ?? 1;
}
