/**
 * Whether a keyboard or pointer event target sits in the library track list
 * (pins ce142ae7e22f / Cmd+A select all must win over deck grid-jump Cmd+A).
 */

export function isLibraryTrackListTarget(target: EventTarget | null): boolean {
	if (target === null || typeof target !== 'object') return false;
	const el = target as { closest?: (selector: string) => unknown };
	if (typeof el.closest !== 'function') return false;
	return (
		el.closest(
			'.table-wrap, [data-testid="track-row"], [data-testid="track-table"], .title-text, .rb-browser'
		) !== null
	);
}
