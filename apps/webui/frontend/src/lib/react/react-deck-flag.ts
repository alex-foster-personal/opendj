/** `ui.react.deck` (default OFF): render the deck's leaf controls through their
 *  React twins (v2 UI migration, REACT-01). Unlike `sets.recording`, this one
 *  FAILS CLOSED: the React path shows only when the daemon DECLARES the flag
 *  on. Unread flags, an undeclared flag, or a flag declared off all keep the
 *  Svelte originals, so a slow or old daemon never swaps the deck's renderer. */
import { buildFlags } from '$lib/api/store-build.svelte';

export const REACT_DECK_FLAG_ID = 'ui.react.deck';

export function reactDeckEnabledIn(
	flags: readonly { flag_id: string; enabled: boolean }[]
): boolean {
	return flags.find((flag) => flag.flag_id === REACT_DECK_FLAG_ID)?.enabled === true;
}

export function reactDeckEnabled(): boolean {
	void buildFlags.load();
	return reactDeckEnabledIn(buildFlags.flags);
}
