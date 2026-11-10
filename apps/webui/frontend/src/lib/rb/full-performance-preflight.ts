/**
 * PERFMODE-02 arm-time preflight: compose host tier facts, machine pressure,
 * xrun window delta, and Gig posture into a single refuse-or-ok decision with
 * named codes only (no UI copy here).
 */

import { resolvedPosture, type AppPosture } from '$lib/rb/app-posture';
import {
	kernelPressureIsElevated,
	PRESSURE_CHURN_EARLY_WARNING,
	readMachinePressure,
	type PressureSnapshot,
	xrunWindowDeltaExceedsMax
} from '$lib/rb/machine-pressure';
import {
	hostInfoReadable,
	resolvedHostLogicalCpus,
	resolvedHostRamGiB,
	resolvedTier,
	resolvedTierSource
} from '$lib/rb/perf-tier';
import { readXrunSessionTotal } from '$lib/rb/xrun-sentinel';

export type FullPerformancePreflightRefuseCode =
	| 'HOST_INFO_UNAVAILABLE'
	| 'KERNEL_ELEVATED'
	| 'PRESSURE_CHURN'
	| 'XRUN_WINDOW'
	| 'POSTURE_NOT_GIG';

export interface FullPerformancePreflightRecorded {
	readonly hostLogicalCpus: number | null;
	readonly hostRamGiB: number | null;
	readonly tier: ReturnType<typeof resolvedTier>;
	readonly tierSource: ReturnType<typeof resolvedTierSource>;
	readonly kernelLevel: number | null;
	readonly churnScore: number | null;
	readonly xrunSessionCount: number;
	readonly xrunWindowDelta: number;
	readonly appPosture: AppPosture;
}

export type FullPerformancePreflightResult =
	| { ok: true; recorded: FullPerformancePreflightRecorded }
	| {
			ok: false;
			code: FullPerformancePreflightRefuseCode;
			recorded: FullPerformancePreflightRecorded;
	  };

export interface FullPerformancePreflightDeps {
	readonly hostInfoReadable: () => boolean;
	readonly readHostLogicalCpus: () => number | null;
	readonly readHostRamGiB: () => number | null;
	readonly resolvedTier: () => ReturnType<typeof resolvedTier>;
	readonly resolvedTierSource: () => ReturnType<typeof resolvedTierSource>;
	readonly readPressure: () => PressureSnapshot | null;
	readonly readXrunCount: () => number;
	readonly xrunWindowBaseline: number;
	readonly resolvedPosture: () => AppPosture;
}

function _churnPressureElevated(snapshot: PressureSnapshot | null): boolean {
	return (
		snapshot !== null &&
		snapshot.churnScore !== null &&
		snapshot.churnScore >= PRESSURE_CHURN_EARLY_WARNING
	);
}

function _xrunWindowDelta(current: number, baseline: number): number {
	return current - baseline;
}

function _recordedFields(
	deps: FullPerformancePreflightDeps,
	pressure: PressureSnapshot | null,
	xrunCount: number,
	xrunWindowDelta: number
): FullPerformancePreflightRecorded {
	return {
		hostLogicalCpus: deps.readHostLogicalCpus(),
		hostRamGiB: deps.readHostRamGiB(),
		tier: deps.resolvedTier(),
		tierSource: deps.resolvedTierSource(),
		kernelLevel: pressure?.kernelLevel ?? null,
		churnScore: pressure?.churnScore ?? null,
		xrunSessionCount: xrunCount,
		xrunWindowDelta,
		appPosture: deps.resolvedPosture()
	};
}

/** Pure preflight over injected reads (unit-tested without the audio graph). */
export function runFullPerformancePreflight(
	deps: FullPerformancePreflightDeps
): FullPerformancePreflightResult {
	const pressure = deps.readPressure();
	const xrunCount = deps.readXrunCount();
	const xrunWindowDelta = _xrunWindowDelta(xrunCount, deps.xrunWindowBaseline);
	const recorded = _recordedFields(deps, pressure, xrunCount, xrunWindowDelta);

	if (!deps.hostInfoReadable()) {
		return { ok: false, code: 'HOST_INFO_UNAVAILABLE', recorded };
	}
	if (kernelPressureIsElevated(pressure)) {
		return { ok: false, code: 'KERNEL_ELEVATED', recorded };
	}
	if (_churnPressureElevated(pressure)) {
		return { ok: false, code: 'PRESSURE_CHURN', recorded };
	}
	if (xrunWindowDeltaExceedsMax(xrunCount, deps.xrunWindowBaseline)) {
		return { ok: false, code: 'XRUN_WINDOW', recorded };
	}
	if (deps.resolvedPosture() !== 'gig') {
		return { ok: false, code: 'POSTURE_NOT_GIG', recorded };
	}
	return { ok: true, recorded };
}

/** Arm-time entry: uses live module singletons and an xrun baseline for the window. */
export function preflightFullPerformanceMode(
	xrunWindowBaseline: number
): FullPerformancePreflightResult {
	return runFullPerformancePreflight({
		hostInfoReadable,
		readHostLogicalCpus: resolvedHostLogicalCpus,
		readHostRamGiB: resolvedHostRamGiB,
		resolvedTier,
		resolvedTierSource,
		readPressure: readMachinePressure,
		readXrunCount: readXrunSessionTotal,
		xrunWindowBaseline,
		resolvedPosture
	});
}
