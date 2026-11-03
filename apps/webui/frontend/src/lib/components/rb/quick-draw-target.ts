/**
 * Which right-clicks the quick-draw menu takes over (parity matrix row 30).
 *
 * Quick-draw owns right-click inside a `.perf-root` surface, so it cancels the
 * browser's own menu there. A text field is the exception: a right-click in
 * the library search box or any other input keeps the platform edit menu (Cut,
 * Copy, Paste, Select All), from the desktop shell or from the browser.
 *
 * [if] a right-click lands on a deck, row or waveform in .perf-root [then]
 *   quick-draw claims it [else stop]
 * [if] a right-click lands in a text input, textarea or contenteditable inside
 *   .perf-root [then] quick-draw leaves it to the edit menu [else stop]
 */
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';

export function quickDrawClaimsContextMenu(target: Element): boolean {
	if (target.closest('.perf-root') === null) return false;
	return !isTextEntryTarget(target);
}
