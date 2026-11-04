/** SET-6586 Routes view (`sets.routes_view`), OFF by default while the view is new.
 *  Unlike `sets.recording`, the controls show only when the daemon DECLARES the
 *  flag on: unread or undeclared flags keep the plain track table. */
import { buildFlags } from '$lib/api/store-build.svelte';

export const SET_ROUTES_VIEW_FLAG_ID = 'sets.routes_view';

export function routesViewEnabledIn(flags: readonly { flag_id: string; enabled: boolean }[]): boolean {
	return flags.find((flag) => flag.flag_id === SET_ROUTES_VIEW_FLAG_ID)?.enabled === true;
}

export function setRoutesViewEnabled(): boolean {
	void buildFlags.load();
	return routesViewEnabledIn(buildFlags.flags);
}
