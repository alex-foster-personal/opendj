/** BrandLaunch overlay duration in ms (launch-fade / mark-arrive CSS). */
export const BRAND_LAUNCH_DURATION_MS = 2700;

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
