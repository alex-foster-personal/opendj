/**
 * Keep a large, never-mutated payload out of Svelte's deep `$state` proxies (B9).
 *
 * A deep `$state` proxies only plain objects and arrays: `proxy()` checks the
 * prototype and returns anything else as-is. Inside a proxy, every property
 * READ creates a signal for that key, plus a nested proxy for every plain
 * object or array under it. An ANLZ payload read through the shared
 * `anlz-cache` `$state` therefore grew about 100,000 signals per track, one
 * per waveform bin, beat and cue. The cache's LRU cap counts the JSON
 * (`ANLZ_ESTIMATE_BYTES`), not these signals. Measured on a dev preview,
 * Tue 6 Oct 2026 (demon-llama, AutoPlay): the post-GC heap grew 15 to 20 MB
 * per deck load, with 1.0 M signal objects and 3.0 M dev label strings
 * retained across 10 loads. That was the maintainer's B9 "preview uses 1.2 GB".
 *
 * `nonReactive` swaps the prototype of the top-level object for one that
 * inherits from `Object.prototype`, so the object behaves the same everywhere
 * (JSON, spread, `in`, `hasOwnProperty`), but Svelte stores it by reference.
 * Reactivity stays on the container: replacing the cache entry still notifies
 * every reader. In exchange, mutating the payload in place is never observed,
 * so call this only on values that are replaced and never edited.
 *
 * Requirements (mini-PRD):
 *   ✔︎ A marked payload is never proxied by a deep $state.
 *     [if] it is read back through a Svelte proxy [then] it is the same object
 *   ✔︎ A marked payload is otherwise unchanged.
 *     [if] it is serialized or spread [then] the result equals the unmarked one
 *   ✔︎ Marking is idempotent and refuses what it cannot keep raw.
 *     [if] it is marked twice [then] the prototype is set once
 *     [if] the value is an array or a class instance [then] it is returned untouched
 */

const NON_REACTIVE_PROTOTYPE: object = Object.freeze(Object.create(Object.prototype));

/** Mark a plain object so a deep `$state` stores it by reference. Returns it. */
export function nonReactive<T extends object>(value: T): T {
	if (Object.getPrototypeOf(value) === Object.prototype) {
		Object.setPrototypeOf(value, NON_REACTIVE_PROTOTYPE);
	}
	return value;
}

/** Whether `value` was marked by {@link nonReactive}. */
export function isNonReactive(value: unknown): boolean {
	return typeof value === 'object' && value !== null && Object.getPrototypeOf(value) === NON_REACTIVE_PROTOTYPE;
}
