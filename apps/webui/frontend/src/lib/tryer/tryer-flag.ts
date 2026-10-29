/** TRYER-14: the try-er page ships behind the `dj.tryer` build flag, default OFF.
 *  It shows only when the daemon DECLARES the flag on; while flags are unread it stays off. */
import { buildFlags } from '$lib/api/store-build.svelte';
import { tryerEnabledIn } from './tryer-model';

export function tryerEnabled(): boolean {
	void buildFlags.load();
	return tryerEnabledIn(buildFlags.flags);
}
