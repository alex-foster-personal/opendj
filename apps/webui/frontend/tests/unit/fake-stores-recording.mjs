/**
 * Test stand-in for `$lib/stores.svelte`: the real module, except that
 * `pushToast()` records its calls on `globalThis.__recordedToasts`. Aliased in
 * through load-typescript.mjs's `alias` option, so every module in the bundle
 * still gets every other real export.
 */
export * from '../../src/lib/stores.svelte.ts';

export function pushToast(message, kind = 'info', dismissMs, cause) {
	if (!Array.isArray(globalThis.__recordedToasts)) globalThis.__recordedToasts = [];
	globalThis.__recordedToasts.push({ message, kind, cause });
}
