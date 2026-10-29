/**
 * Lazy loader for the library key hover card (KEYSHIFT-04). The card loads on
 * the first key hover, so it stays out of the library route's initial bundle.
 */
export function loadKeyHoverCard(): Promise<typeof import('./KeyHoverCard.svelte')> {
	return import('./KeyHoverCard.svelte');
}
