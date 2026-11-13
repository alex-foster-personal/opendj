/**
 * Selector node ids for the transient /performance banners (#14559, epic #9372).
 *
 * While shown, each banner is a selector group holding one leaf per button it shows, so a
 * keyboard-only operator reaches "Resume", "Take over all" and the like with the selector
 * keys, and Enter on a leaf presses that one button. Banners sit outside every section, so
 * they register as roots and the spatial order places them by where they sit on screen.
 * Ids are `banner.<name>` and `banner.<name>.<action>`; a hidden banner registers nothing.
 */

/** Each banner and the actions of its buttons, in on-screen order. */
export const BANNER_SEL_ACTIONS = {
	'reload-resume': ['resume', 'dismiss'],
	'autoplay-stall': ['tracks', 'dismiss'],
	'replay-takeover': ['take-over-all'],
	'tab-leader': ['take-control']
} as const;

export type BannerSelName = keyof typeof BANNER_SEL_ACTIONS;
export type BannerSelAction<N extends BannerSelName> = (typeof BANNER_SEL_ACTIONS)[N][number];

/** Group id of one banner. */
export function bannerSelId(name: BannerSelName): string {
	return `banner.${name}`;
}

/** Leaf id of one button inside a banner's group. */
export function bannerLeafSelId<N extends BannerSelName>(
	name: N,
	action: BannerSelAction<N>
): string {
	return `${bannerSelId(name)}.${action}`;
}
