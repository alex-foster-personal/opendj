/** Test stub for `$lib/stores.svelte`'s `pushToast()`. */
export function pushToast(message, kind, dismissMs, cause, context, groupKey) {
	if (!Array.isArray(globalThis.__shellNavToastCalls)) {
		globalThis.__shellNavToastCalls = [];
	}
	globalThis.__shellNavToastCalls.push({ message, kind, groupKey });
}
