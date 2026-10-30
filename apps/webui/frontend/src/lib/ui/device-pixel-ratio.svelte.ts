/**
 * The window's devicePixelRatio as reactive state, for code that snaps to
 * device pixels (pixel-snap.ts). A plain `window.devicePixelRatio` read is not
 * tracked, so moving the window to a monitor of another density, or zooming,
 * would leave a snapped playhead on the old grid until something else changed.
 *
 * Server rendering has no device grid, so it reports SSR_DEVICE_PIXEL_RATIO on
 * purpose; the browser value replaces it on the first read after hydration. In
 * the browser the ratio must be a finite positive number: zero, NaN or a
 * missing value throws rather than passing for a plausible 1x density.
 */
export const SSR_DEVICE_PIXEL_RATIO = 1;

/** The browser's devicePixelRatio, or a thrown RangeError when it is not usable. */
export function readDevicePixelRatio(win: Pick<Window, 'devicePixelRatio'>): number {
	const ratio = win.devicePixelRatio;
	if (typeof ratio !== 'number' || !Number.isFinite(ratio) || ratio <= 0) {
		throw new RangeError(`devicePixelRatio must be a finite positive number, got ${String(ratio)}`);
	}
	return ratio;
}

// Read once at module load, never inside a read: devicePixelRatioNow() runs in
// component $deriveds, where assigning $state throws state_unsafe_mutation.
let dpr = $state(typeof window === 'undefined' ? SSR_DEVICE_PIXEL_RATIO : readDevicePixelRatio(window));
let watching = false;

/**
 * Start following density changes. Call it from component init (StripWaveform,
 * JogDial), never from a $derived: it must be listening before any playhead is
 * read, so a zoom or display move while every deck is empty is not missed.
 * Idempotent, and a no-op during server rendering.
 */
export function startDevicePixelRatioWatch(): void {
	if (watching || typeof window === 'undefined') return;
	// Without matchMedia a density change would never reach the snap grid; say so
	// rather than mark the watcher armed with nothing listening.
	if (typeof window.matchMedia !== 'function') {
		throw new TypeError('startDevicePixelRatioWatch: window.matchMedia is unavailable, so density changes cannot be tracked');
	}
	// Armed only once the live read validated and the listener is in place, so a
	// throw here leaves the next start free to try again.
	syncAndArm(window);
	watching = true;
}

/**
 * The one place the ratio is read after module load: take the LIVE value,
 * validate it, publish it if it moved, and arm the query on that same value.
 * Start and every re-arm go through here, so the query can never be armed on a
 * cached ratio that the window has already left (Sol P1s on #8046).
 */
function syncAndArm(win: Window): void {
	const live = readDevicePixelRatio(win);
	if (live !== dpr) dpr = live;
	// A resolution query matches one density, so re-arm on every change.
	win.matchMedia(`(resolution: ${live}dppx)`).addEventListener('change', () => syncAndArm(win), { once: true });
}

/**
 * Current devicePixelRatio. A pure read: inside a $derived it tracks density
 * changes once startDevicePixelRatioWatch() has run, and it writes no state.
 */
export function devicePixelRatioNow(): number {
	return dpr;
}
