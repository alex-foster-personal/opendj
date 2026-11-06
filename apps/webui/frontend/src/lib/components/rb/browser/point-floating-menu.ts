import { tick } from 'svelte';

/** Focus, Escape, and outside-press for a pointFloatingAction menu.
 *  Placement stays on pointFloatingAction in `$lib/ui/clamp-to-viewport`:
 *  these menus open on a single loading row and grow once items arrive, so a
 *  clamp measured at open left the grown menu hanging off the viewport
 *  bottom (unclickable - a fixed node cannot be scrolled into view). The
 *  action re-clamps from (x, y) on every size change.
 *
 *  Shared by RelocatePopoverView and TrackPlaylistsPopoverView (issue #8502).
 *  Supersedes RelocatePopoverView.svelte typescript ~27-58 and
 *  TrackPlaylistsPopoverView.svelte typescript ~22-53.
 */

export async function focusPointFloatingMenu(el: HTMLElement | null): Promise<void> {
	await tick();
	el?.focus();
}

export function closePointFloatingMenuOnEscape(event: KeyboardEvent, onclose: () => void): void {
	if (event.key === 'Escape') {
		event.preventDefault();
		onclose();
	}
}

export function closePointFloatingMenuOnOutsidePointer(
	event: PointerEvent,
	menu: HTMLElement | null,
	onclose: () => void
): void {
	if (menu !== null && event.target instanceof Node && !menu.contains(event.target)) onclose();
}
