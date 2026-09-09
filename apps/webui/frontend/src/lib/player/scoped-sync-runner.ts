/**
 * Split out of audio-engine.svelte.ts (T4: that file sits at the
 * file_size.max_frontend ratchet floor) so the install-once/run-many
 * indirection for a caller-owned scoped scheduler is not reinvented inline.
 *
 * A deferred continuation (e.g. the beatgrid resync in audio-engine.svelte.ts)
 * fires after its caller already released the scope it needs, so it must
 * reclaim that scope itself rather than run its task unscoped. The concrete
 * scheduler lives in performance-ipc.svelte.ts and is wired in once via
 * `install`; `run` throws if nothing has installed one yet, since running a
 * scope-sensitive task unscoped is the exact bug this exists to prevent.
 *
 * `run` is always claimed for `key` alone first. `widen` (r3913096141 /
 * r3913350814) lets the task escalate to the installed runner's WIDER
 * barrier from INSIDE that claim, once it has actually started - never
 * before. A caller that decides "do I need the wide barrier?" before
 * scheduling can be answered by state that has since changed: `key`'s own
 * claim can queue behind an unrelated in-flight command that changes the
 * answer between scheduling and execution (e.g. a PLAY on the same deck
 * electing it sync master). Deciding inside `run`, after `key`'s scope is
 * already held, reads state no earlier command can still be racing to
 * change. `widen`'s scopes MUST include `key` itself (r3914267990): the
 * installed runner's own `run([key], ...)` claim must never await or return
 * what `widen` returns, and it is exactly that fire-and-forget shape that
 * makes re-including `key` safe rather than a self-await - the wide claim's
 * predecessors are computed (from the still-current tail map) before its own
 * registration overwrites it, so it correctly waits for the in-flight `key`
 * claim rather than depending on itself, and once registered it becomes the
 * new holder of `key` for anything submitted after. Excluding `key` leaves a
 * gap, the instant the narrow claim's body returns, where a fresh command on
 * the same key can run concurrently with the still in-flight widened work.
 */
export type WidenScope = (work: () => Promise<void>) => Promise<void>;
export type ScopedRunner<TKey> = (key: TKey, run: (widen: WidenScope) => Promise<void>) => Promise<void>;

export interface ScopedSyncRunner<TKey> {
	install(runner: ScopedRunner<TKey>): void;
	run(key: TKey, task: (widen: WidenScope) => Promise<void>): Promise<void>;
}

export function createScopedSyncRunner<TKey>(): ScopedSyncRunner<TKey> {
	let runner: ScopedRunner<TKey> | null = null;
	return {
		install(next) {
			runner = next;
		},
		run(key, task) {
			if (runner === null) {
				throw new Error('scoped sync runner was never installed');
			}
			return runner(key, task);
		}
	};
}
