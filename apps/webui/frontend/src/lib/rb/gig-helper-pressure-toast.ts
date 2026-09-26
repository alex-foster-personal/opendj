/**
 * Gig helper pressure toasts (PERFMODE-16). Reuses cached machine-pressure snapshots only.
 */

import type { PressureSnapshot } from './machine-pressure';

export type GigHelperPressureToastDeps = {
	pushToast: (message: string, kind: 'warn') => void;
};

let _inElevatedEpisode = false;

/** Test hook: reset episode tracking between cases. */
export function resetGigHelperPressureEpisode(): void {
	_inElevatedEpisode = false;
}

export function onGigHelperPressureSnapshot(
	snapshot: PressureSnapshot | null,
	deps: GigHelperPressureToastDeps
): void {
	const band = snapshot?.band ?? null;
	if (band !== 'warning' && band !== 'critical') {
		_inElevatedEpisode = false;
		return;
	}
	if (_inElevatedEpisode) return;
	_inElevatedEpisode = true;
	deps.pushToast(
		`Gig helper: system pressure is ${band}. Open PerfMeters for live readings.`,
		'warn'
	);
}
