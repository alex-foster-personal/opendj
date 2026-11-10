// requirement: PERFMODE-02 preflight (issue #11238)

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const BASE_DEPS = {
	hostInfoReadable: () => true,
	readHostLogicalCpus: () => 8,
	readHostRamGiB: () => 16,
	resolvedTier: () => 'STANDARD',
	resolvedTierSource: () => 'auto',
	readPressure: () => ({
		loadAvg1m: 1.2,
		memFreeMb: 4096,
		swapUsedMb: 0,
		kernelLevel: 1,
		kernelMemoryPressureLevel: 1,
		churnScore: 10,
		swapRate: null,
		decompRate: null,
		band: 'fine',
		sampleIntervalMs: 1000,
		compressedMb: null,
		requestedAtMs: 0,
		serverCacheAgeMs: 0
	}),
	readXrunCount: () => 0,
	xrunWindowBaseline: 0,
	resolvedPosture: () => 'gig'
};

test('[if] host-info unreadable [then] preflight refuses HOST_INFO_UNAVAILABLE', async () => {
	const { runFullPerformancePreflight } = await loadTypeScriptModule(
		'src/lib/rb/full-performance-preflight.ts'
	);
	const result = runFullPerformancePreflight({
		...BASE_DEPS,
		hostInfoReadable: () => false,
		readHostLogicalCpus: () => null,
		readHostRamGiB: () => null
	});
	assert.equal(result.ok, false);
	assert.equal(result.code, 'HOST_INFO_UNAVAILABLE');
});

test('[if] kernel_level is 2 or above [then] preflight refuses KERNEL_ELEVATED', async () => {
	const { runFullPerformancePreflight } = await loadTypeScriptModule(
		'src/lib/rb/full-performance-preflight.ts'
	);
	const result = runFullPerformancePreflight({
		...BASE_DEPS,
		readPressure: () => ({
			...BASE_DEPS.readPressure(),
			kernelLevel: 2,
			kernelMemoryPressureLevel: 2
		})
	});
	assert.equal(result.ok, false);
	assert.equal(result.code, 'KERNEL_ELEVATED');
});

test('[if] churn_score is 500 or above [then] preflight refuses PRESSURE_CHURN', async () => {
	const { runFullPerformancePreflight } = await loadTypeScriptModule(
		'src/lib/rb/full-performance-preflight.ts'
	);
	const result = runFullPerformancePreflight({
		...BASE_DEPS,
		readPressure: () => ({
			...BASE_DEPS.readPressure(),
			churnScore: 500
		})
	});
	assert.equal(result.ok, false);
	assert.equal(result.code, 'PRESSURE_CHURN');
});

test('[if] xrun delta exceeds S1_XRUN_DELTA_MAX [then] preflight refuses XRUN_WINDOW', async () => {
	const { runFullPerformancePreflight } = await loadTypeScriptModule(
		'src/lib/rb/full-performance-preflight.ts'
	);
	const { S1_XRUN_DELTA_MAX } = await loadTypeScriptModule('src/lib/rb/machine-pressure.ts');
	const result = runFullPerformancePreflight({
		...BASE_DEPS,
		readXrunCount: () => S1_XRUN_DELTA_MAX + 1,
		xrunWindowBaseline: 0
	});
	assert.equal(result.ok, false);
	assert.equal(result.code, 'XRUN_WINDOW');
});

test('[if] app_posture is not gig [then] preflight refuses POSTURE_NOT_GIG', async () => {
	const { runFullPerformancePreflight } = await loadTypeScriptModule(
		'src/lib/rb/full-performance-preflight.ts'
	);
	const result = runFullPerformancePreflight({
		...BASE_DEPS,
		resolvedPosture: () => 'prep'
	});
	assert.equal(result.ok, false);
	assert.equal(result.code, 'POSTURE_NOT_GIG');
});

test('[if] every check is clean [then] preflight returns ok and records fields', async () => {
	const { runFullPerformancePreflight } = await loadTypeScriptModule(
		'src/lib/rb/full-performance-preflight.ts'
	);
	const result = runFullPerformancePreflight(BASE_DEPS);
	assert.equal(result.ok, true);
	assert.equal(result.recorded.hostLogicalCpus, 8);
	assert.equal(result.recorded.hostRamGiB, 16);
	assert.equal(result.recorded.tier, 'STANDARD');
	assert.equal(result.recorded.tierSource, 'auto');
	assert.equal(result.recorded.kernelLevel, 1);
	assert.equal(result.recorded.churnScore, 10);
	assert.equal(result.recorded.xrunSessionCount, 0);
	assert.equal(result.recorded.xrunWindowDelta, 0);
	assert.equal(result.recorded.appPosture, 'gig');
});
