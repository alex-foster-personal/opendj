/**
 * One place every capped cache re-applies its caps from.
 *
 * A cache whose budget depends on the machine (PERFMODE-01 tier), the posture
 * (PERFMODE-03 Gig/Prep) or live pressure (PERFMODE-04 Q29 stepper) has to
 * re-evict whenever any of those change. Before this module each cache's
 * `apply*Caps` was called by hand from every place a cap could change, and
 * they had drifted: the audio prefetch cache was re-applied from four places,
 * the ANLZ cache from only one (tier resolve), and the CUEOUT-15 preview cache
 * from none until it was wired in by hand. A new cache now registers ONCE,
 * next to its own state, and every cap change reaches it.
 *
 * Mini-PRD:
 * - ✔︎ R1 `applyAllCaps()` runs every registered consumer.
 *   - [if] a cache registers [then ⛔️] the next applyAllCaps runs its apply
 * - ✔︎ R2 Registration is idempotent by name, so a Vite HMR re-evaluation of a
 *   cache module replaces its consumer instead of throwing or duplicating.
 *   - [if] the same name registers twice [then ⛔️] only the newer apply runs
 * - ✔︎ R3 One throwing consumer does not stop the others evicting, and the
 *   failure still surfaces.
 *   - [if] a consumer throws [then ⛔️] later consumers still run and
 *     applyAllCaps throws an AggregateError naming the consumer
 * - ✔︎ R4 The unregister handle removes only its own registration.
 *   - [if] an old handle is called after a re-registration [then ⛔️] the newer
 *     consumer stays registered
 *
 * A cache module registers at module load. One that has not loaded yet holds
 * nothing to evict, and reads its caps live when it does load, so there is no
 * ordering hazard in registering late.
 */

const _consumers = new Map<string, () => void>();

/** Register a cache's re-evict. Returns an unregister handle. */
export function registerCapsConsumer(name: string, apply: () => void): () => void {
	_consumers.set(name, apply);
	return () => {
		if (_consumers.get(name) === apply) _consumers.delete(name);
	};
}

/** Re-apply every registered cache's caps. Call after any cap input changes. */
export function applyAllCaps(): void {
	const failures: Error[] = [];
	for (const [name, apply] of _consumers) {
		try {
			apply();
		} catch (error) {
			failures.push(new Error(`caps consumer "${name}" failed: ${String(error)}`, { cause: error }));
		}
	}
	if (failures.length > 0) {
		throw new AggregateError(failures, `${failures.length} caps consumer(s) failed`);
	}
}
