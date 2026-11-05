/** AISIDE-7892j: the agent sidebar ships behind the `assistant.sidebar` build flag,
 *  default OFF. It shows only when the daemon DECLARES the flag on; while the
 *  flags are unread, or the daemon is older and does not declare it, there is
 *  no tab, no chord and no hotkeys row. */
import { buildFlags } from '$lib/api/store-build.svelte';

export const ASSISTANT_SIDEBAR_FLAG_ID = 'assistant.sidebar';

export function assistantSidebarEnabledIn(
	flags: readonly { flag_id: string; enabled: boolean }[]
): boolean {
	return flags.find((flag) => flag.flag_id === ASSISTANT_SIDEBAR_FLAG_ID)?.enabled === true;
}

export function assistantSidebarEnabled(): boolean {
	void buildFlags.load();
	return assistantSidebarEnabledIn(buildFlags.flags);
}
