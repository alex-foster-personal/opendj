/** DECKUX-6794: snapped deck jumps ship behind the `deck.snapped_jump` build
 *  flag, default OFF. The keys only bind when the daemon DECLARES the flag on;
 *  while the flags are unread, or the daemon does not declare it, Cmd+arrows
 *  stay unbound on decks. */
import { buildFlags } from '$lib/api/store-build.svelte';

export const GRID_JUMP_FLAG_ID = 'deck.snapped_jump';

export function gridJumpEnabledIn(
	flags: readonly { flag_id: string; enabled: boolean }[]
): boolean {
	return flags.find((flag) => flag.flag_id === GRID_JUMP_FLAG_ID)?.enabled === true;
}

export function gridJumpEnabled(): boolean {
	void buildFlags.load();
	return gridJumpEnabledIn(buildFlags.flags);
}
