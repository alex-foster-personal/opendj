/**
 * PERFMODE-05 (issue #1987): PerfMeters v2 pure model.
 *
 * Regression: omit-if-absent swap/pressure, elevated sampler interval,
 * sparkline gaps for null samples, unnamed process members stay listed.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let model;

before(async () => {
	model = await loadTypeScriptModule('src/lib/rb/perf-meter-model.ts');
});

test('perfMeterSampleIntervalMs uses idle 1000 by default', () => {
	assert.equal(model.perfMeterSampleIntervalMs({ kernelLevel: null, churnScore: null }), 1000);
	assert.equal(model.perfMeterSampleIntervalMs({ kernelLevel: 1, churnScore: 0 }), 1000);
});

test('perfMeterSampleIntervalMs lengthens to 5000 when kernel is elevated', () => {
	assert.equal(model.perfMeterSampleIntervalMs({ kernelLevel: 2, churnScore: null }), 5000);
	assert.equal(model.perfMeterSampleIntervalMs({ kernelLevel: 4, churnScore: 0 }), 5000);
});

test('perfMeterSampleIntervalMs lengthens to 5000 when churn is elevated', () => {
	assert.equal(model.perfMeterSampleIntervalMs({ kernelLevel: null, churnScore: 500 }), 5000);
	assert.equal(model.perfMeterSampleIntervalMs({ kernelLevel: 1, churnScore: 600 }), 5000);
});

test('breakdownLines omits swap when swapMB is null', () => {
	const lines = model.breakdownLines({
		hz: 60,
		hzQualityOk: true,
		hzTickHz: 60,
		prefetchCount: 0,
		prefetchMB: 0,
		anlzCount: 0,
		anlzMB: 0,
		pcmMB: 100,
		jsHeapMB: 200,
		ringCount: 0,
		swapMB: null,
		kernelLevel: null,
		churnScore: null,
		compressorRate: null,
		processes: null
	});
	assert.ok(!lines.some((line) => line.key === 'swap'));
});

test('breakdownLines includes swap when measured', () => {
	const lines = model.breakdownLines({
		hz: 60,
		hzQualityOk: true,
		hzTickHz: 60,
		prefetchCount: 0,
		prefetchMB: 0,
		anlzCount: 0,
		anlzMB: 0,
		pcmMB: 100,
		jsHeapMB: 200,
		ringCount: 0,
		swapMB: 12,
		kernelLevel: null,
		churnScore: null,
		compressorRate: null,
		processes: null
	});
	const swap = lines.find((line) => line.key === 'swap');
	assert.ok(swap);
	assert.match(swap.value, /12/);
});

test('breakdownLines omits kernel pressure when null', () => {
	const lines = model.breakdownLines({
		hz: 60,
		hzQualityOk: true,
		hzTickHz: 60,
		prefetchCount: 0,
		prefetchMB: 0,
		anlzCount: 0,
		anlzMB: 0,
		pcmMB: 100,
		jsHeapMB: 200,
		ringCount: 0,
		swapMB: null,
		kernelLevel: null,
		churnScore: null,
		compressorRate: null,
		processes: null
	});
	assert.ok(!lines.some((line) => line.key === 'kernel-pressure'));
});

test('breakdownLines includes kernel pressure when present', () => {
	const lines = model.breakdownLines({
		hz: 60,
		hzQualityOk: true,
		hzTickHz: 60,
		prefetchCount: 0,
		prefetchMB: 0,
		anlzCount: 0,
		anlzMB: 0,
		pcmMB: 100,
		jsHeapMB: 200,
		ringCount: 0,
		swapMB: null,
		kernelLevel: 2,
		churnScore: null,
		compressorRate: null,
		processes: null
	});
	const kernel = lines.find((line) => line.key === 'kernel-pressure');
	assert.ok(kernel);
	assert.equal(kernel.value, '2');
});

test('breakdownLines keeps unnamed process members in the list', () => {
	const lines = model.breakdownLines({
		hz: 60,
		hzQualityOk: true,
		hzTickHz: 60,
		prefetchCount: 0,
		prefetchMB: 0,
		anlzCount: 0,
		anlzMB: 0,
		pcmMB: 100,
		jsHeapMB: 200,
		ringCount: 0,
		swapMB: null,
		kernelLevel: null,
		churnScore: null,
		compressorRate: null,
		processes: {
			kernelLevel: null,
			churnScore: null,
			compressorRate: null,
			members: [{ label: 'unnamed', mb: 42 }]
		}
	});
	const unnamed = lines.find((line) => line.label === 'unnamed');
	assert.ok(unnamed);
	assert.match(unnamed.value, /42/);
});

test('breakdownLines keeps two unnamed members as separate rows', () => {
	const lines = model.breakdownLines({
		hz: 60,
		hzQualityOk: true,
		hzTickHz: 60,
		prefetchCount: 0,
		prefetchMB: 0,
		anlzCount: 0,
		anlzMB: 0,
		pcmMB: 100,
		jsHeapMB: 200,
		ringCount: 0,
		swapMB: null,
		kernelLevel: null,
		churnScore: null,
		compressorRate: null,
		processes: {
			kernelLevel: null,
			churnScore: null,
			compressorRate: null,
			members: [
				{ label: 'unnamed', mb: 10 },
				{ label: 'unnamed', mb: 20 }
			]
		}
	});
	const unnamed = lines.filter((line) => line.label === 'unnamed');
	assert.equal(unnamed.length, 2);
	assert.notEqual(unnamed[0].key, unnamed[1].key);
});

test('breakdownLines shows unavailable rather than 0 when member mb is missing', () => {
	const lines = model.breakdownLines({
		hz: 60,
		hzQualityOk: true,
		hzTickHz: 60,
		prefetchCount: 0,
		prefetchMB: 0,
		anlzCount: 0,
		anlzMB: 0,
		pcmMB: 100,
		jsHeapMB: 200,
		ringCount: 0,
		swapMB: null,
		kernelLevel: null,
		churnScore: null,
		compressorRate: null,
		processes: {
			kernelLevel: null,
			churnScore: null,
			compressorRate: null,
			members: [{ label: 'unnamed', mb: null }]
		}
	});
	const unnamed = lines.find((line) => line.label === 'unnamed');
	assert.ok(unnamed);
	assert.equal(unnamed.value, 'unavailable');
});

test('sparkPoints does not turn null into 0', () => {
	const geometry = model.sparkPoints([null, 5, null, 10]);
	assert.equal(geometry.segments.length, 1, 'null gaps must split segments');
	assert.ok(!geometry.segments[0].points.includes(',0 '), 'null must not plot as zero');
});

test('pushSample caps the ring', () => {
	const ring = model.pushSample([1, 2, 3], 4, 3);
	assert.deepEqual(ring, [2, 3, 4]);
});
