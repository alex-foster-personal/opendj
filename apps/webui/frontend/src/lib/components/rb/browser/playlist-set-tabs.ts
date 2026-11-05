/** SET-05 tab label helper: ``AtlantisParty (3)``. */
export function setTabLabel(name: string, playCount: number): string {
	return `${name} (${playCount})`;
}

/**
 * SET-6586: the set the tabs should select after the Routes view's selection moved. Only a change on
 * the route side counts: a set that is newly selected there and still exists is followed, and anything
 * else leaves the tabs alone, so a newer tab choice is never pulled back to an older route selection.
 */
export function routeSetToFollow(
	routeSetId: number | null,
	previousRouteSetId: number | null,
	selectedId: number | null,
	setIds: readonly number[]
): number | null {
	if (routeSetId === null || routeSetId === previousRouteSetId || routeSetId === selectedId)
		return null;
	return setIds.includes(routeSetId) ? routeSetId : null;
}
