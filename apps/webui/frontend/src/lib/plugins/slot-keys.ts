// The two helpers the topbar's slot host needs. Kept out of plugin-status.ts, which
// only the /plugins page imports, so the always-on topbar does not carry that page's helpers.

/** Share one in-flight or finished load, but forget a failed one so the next call asks again. */
export function memoUntilFailure(load: () => Promise<void>): () => Promise<void> {
	let pending: Promise<void> | null = null;
	return () =>
		(pending ??= load().catch((exc: unknown) => {
			pending = null;
			throw exc;
		}));
}

/** A collision-free key for one loaded module in a slot: ids never contain '@'. */
export function slotKey(slot: { plugin_id: string; version: string }): string {
	return `${slot.plugin_id}@${slot.version}`;
}
