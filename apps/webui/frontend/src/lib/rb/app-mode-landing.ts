/**
 * PERFMODE-11: cold-open landing route from the last Gig stamp.
 * PERFMODE-12 will flip `LIBRARY_MODE_SHIPPED` when Library mode ships.
 */

export const GIG_LANDING_ROUTE = '/performance';
export const LIBRARY_LANDING_ROUTE = '/';
export const GIG_RETURN_WINDOW_MS = 24 * 60 * 60 * 1000;

/** Flip when PERFMODE-14 lands; PERFMODE-12 reads this for the stale-stamp branch. */
export const LIBRARY_MODE_SHIPPED = false;

export function parseLastGigAt(raw: string | null | undefined): Date | null {
	if (raw === null || raw === undefined) return null;
	if (typeof raw !== 'string') return null;
	const parsed = Date.parse(raw);
	if (!Number.isFinite(parsed)) return null;
	return new Date(parsed);
}

/** Window is exclusive at exactly 24 h: `now - stamp < GIG_RETURN_WINDOW_MS`. */
export function isLastGigWithinWindow(lastGigAt: Date | null, nowMs: number): boolean {
	if (lastGigAt === null) return false;
	const ageMs = nowMs - lastGigAt.getTime();
	return ageMs >= 0 && ageMs < GIG_RETURN_WINDOW_MS;
}

export function resolveBootLandingRoute(
	lastGigAtRaw: string | null | undefined,
	nowMs: number,
	opts?: { libraryModeShipped?: boolean }
): typeof GIG_LANDING_ROUTE | typeof LIBRARY_LANDING_ROUTE {
	const parsed = parseLastGigAt(lastGigAtRaw);
	if (isLastGigWithinWindow(parsed, nowMs)) {
		return GIG_LANDING_ROUTE;
	}
	const libraryShipped = opts?.libraryModeShipped ?? LIBRARY_MODE_SHIPPED;
	if (!libraryShipped) {
		return GIG_LANDING_ROUTE;
	}
	return LIBRARY_LANDING_ROUTE;
}
