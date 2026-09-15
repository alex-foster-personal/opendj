/** BrandLaunch slide phase: halves apart -> closed circle (ms). */
export const BRAND_LAUNCH_SLIDE_MS = 900;
/** Closed-circle hold before fade begins (ms). */
export const BRAND_LAUNCH_HOLD_MS = 200;
/** Whole-mark fade after hold (ms). */
export const BRAND_LAUNCH_FADE_MS = 500;
/** Total overlay duration from first paint to fully faded (ms). */
export const BRAND_LAUNCH_DURATION_MS =
	BRAND_LAUNCH_SLIDE_MS + BRAND_LAUNCH_HOLD_MS + BRAND_LAUNCH_FADE_MS;
/** When the slide completes and the circle closes (ms). */
export const BRAND_LAUNCH_MEET_MS = BRAND_LAUNCH_SLIDE_MS;
/** When the fade phase begins (ms). */
export const BRAND_LAUNCH_FADE_START_MS = BRAND_LAUNCH_SLIDE_MS + BRAND_LAUNCH_HOLD_MS;

/** The durable, per-browser completion marker for the OPS-10 identity launch. */
const BRAND_LAUNCH_STORAGE_KEY = 'odj.brand-launch.v1';
const BRAND_LAUNCH_COMPLETE = 'complete';

/** A launch only replays when no prior run reached its settled logo state. */
export function shouldPlayBrandLaunch(storage: Pick<Storage, 'getItem'>): boolean {
	return storage.getItem(BRAND_LAUNCH_STORAGE_KEY) !== BRAND_LAUNCH_COMPLETE;
}

/** Persist completion only after the visible animation has reached its end. */
export function completeBrandLaunch(storage: Pick<Storage, 'setItem'>): void {
	storage.setItem(BRAND_LAUNCH_STORAGE_KEY, BRAND_LAUNCH_COMPLETE);
}
