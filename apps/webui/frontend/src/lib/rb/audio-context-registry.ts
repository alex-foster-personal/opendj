/**
 * Tracks live AudioContext instances owned by the Gig audio engine.
 *
 * Used by PERFMODE-14 library-mode teardown to prove no deck graph survives
 * a mode switch. Registration is explicit from engine construction/disposal
 * only; nothing here creates or closes contexts.
 */

const _registered = new Set<AudioContext>();

export function registerAudioContext(ctx: AudioContext): void {
	_registered.add(ctx);
}

export function unregisterAudioContext(ctx: AudioContext): void {
	_registered.delete(ctx);
}

export function countRegisteredAudioContexts(): number {
	return _registered.size;
}

/** Test-only reset when a module singleton outlives the case. */
export function resetAudioContextRegistryForTest(): void {
	_registered.clear();
}
