/** CMDK-04: the redesigned Cmd K bar ships behind the `cmdk.v2` build flag,
 *  default OFF. It shows only when the daemon DECLARES the flag on; while the
 *  flags are unread, or the daemon is older and does not declare it, the
 *  shipped CMDK-01..03 bar stays. */
import { buildFlags } from '$lib/api/store-build.svelte';

export const CMDK_V2_FLAG_ID = 'cmdk.v2';

export function cmdkV2EnabledIn(flags: readonly { flag_id: string; enabled: boolean }[]): boolean {
	return flags.find((flag) => flag.flag_id === CMDK_V2_FLAG_ID)?.enabled === true;
}

export function cmdkV2Enabled(): boolean {
	void buildFlags.load();
	return cmdkV2EnabledIn(buildFlags.flags);
}
