/** `shell.backstage` (default OFF): reads the daemon's declared build flags.
 *  Off while flags are unread or the daemon does not declare it: hidden only
 *  until the daemon DECLARES the flag on. The id is inlined to keep the root
 *  layout's initial bundle small; a unit test pins the copies equal. */
import { buildFlags } from '$lib/api/store-build.svelte';

export function backstageEnabled(): boolean {
	void buildFlags.load();
	return buildFlags.flags.find((flag) => flag.flag_id === 'shell.backstage')?.enabled === true;
}
