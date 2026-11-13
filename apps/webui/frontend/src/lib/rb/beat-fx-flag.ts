/** `fx.beat_rack` (issue #9782), OFF by default: the beat FX unit graph is built only when
 *  the daemon DECLARES the flag on; unread or undeclared flags build today's graph. */
import { buildFlags } from '$lib/api/store-build.svelte';

export const BEAT_FX_RACK_FLAG_ID = 'fx.beat_rack';

/** True only when `flags` carries `fx.beat_rack` enabled. */
export function beatFxRackEnabledIn(
	flags: readonly { flag_id: string; enabled: boolean }[]
): boolean {
	return flags.find((flag) => flag.flag_id === BEAT_FX_RACK_FLAG_ID)?.enabled === true;
}

/** The flag as already loaded; reading it never starts a fetch from inside a graph build. */
export function beatFxRackEnabled(): boolean {
	return beatFxRackEnabledIn(buildFlags.flags);
}
