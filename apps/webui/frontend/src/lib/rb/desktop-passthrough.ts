/**
 * Tech-mode desktop passthrough for /performance (html/body transparency).
 *
 * app.css paints `html, body` with an opaque --bg unconditionally (every
 * other route wants that). .perf-root going transparent while tech mode is
 * active is not enough on its own - html/body sit BEHIND it and still painted
 * solid, so hidden regions revealed the app's own dark body instead of
 * whatever is behind the window. Toggled on the html/body elements directly
 * (not reachable from .perf-root's own scoped CSS, which can only select
 * descendants) and cleaned up on route change so no other route inherits a
 * transparent body.
 */

const DESKTOP_PASSTHROUGH_CLASS = 'tw-desktop-passthrough';

interface ClassListHost {
	classList: { toggle(token: string, force?: boolean): boolean; remove(token: string): void };
}

/** Apply the passthrough class for `active`; the returned cleanup always
 * removes it (the `$effect` teardown on re-run and on route change). */
export function applyDesktopPassthrough(
	active: boolean,
	elements: readonly ClassListHost[] = [document.documentElement, document.body]
): () => void {
	for (const el of elements) el.classList.toggle(DESKTOP_PASSTHROUGH_CLASS, active);
	return () => {
		for (const el of elements) el.classList.remove(DESKTOP_PASSTHROUGH_CLASS);
	};
}
