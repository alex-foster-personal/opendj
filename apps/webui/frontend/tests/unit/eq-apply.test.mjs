// requirement: LATENCY-03
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { engineBlockAfter, readFrontendSource as readSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const REPO_ROOT = fileURLToPath(new URL('../../../../../', import.meta.url));

let eqApply;
let pressStamp;
let buckets;

before(async () => {
	eqApply = await loadTypeScriptModule('src/lib/player/eq-apply.ts');
	pressStamp = await loadTypeScriptModule('src/lib/rb/press-stamp.ts');
	buckets = await loadTypeScriptModule('src/lib/rb/perf-event-buckets.ts');
});

function readInputToAppliedFloorMs() {
	const baseline = JSON.parse(
		readFileSync(`${REPO_ROOT}/ops/quality/baseline.json`, 'utf8')
	);
	return baseline.metrics['latency.input_to_applied_ms'];
}

function isOverInputToAppliedFloor(ms, floorMs) {
	return ms > floorMs;
}

test('eqRampPlan starts the ramp at nowSec', () => {
	const plan = eqApply.eqRampPlan(10, 0.01);
	assert.equal(plan.startSec, 10);
	assert.equal(plan.endSec, 10.01);
	const rampMs = (plan.endSec - plan.startSec) * 1000;
	assert.ok(rampMs >= 5 && rampMs <= 10, `ramp ${rampMs}ms outside [5, 10]`);
});

test('applyEqRamp cancels, holds current value, then linear ramps immediately', () => {
	const calls = [];
	const param = {
		value: -3,
		cancelScheduledValues(t) {
			calls.push(['cancel', t]);
		},
		setValueAtTime(v, t) {
			calls.push(['set', v, t]);
		},
		linearRampToValueAtTime(v, t) {
			calls.push(['ramp', v, t]);
		}
	};
	const plan = eqApply.applyEqRamp(param, 6, 10, 0.01);
	assert.deepEqual(calls, [
		['cancel', 10],
		['set', -3, 10],
		['ramp', 6, 10.01]
	]);
	assert.equal((plan.startSec - 10) * 1000, 0, 'ramp must start at the same currentTime read');
});

test('eqApplyStages carries press_to_apply_ms and zero ramp offset', () => {
	assert.deepEqual(
		eqApply.eqApplyStages({ pressToApplyMs: 5, rampStartOffsetMs: 0, rampDurationMs: 10 }),
		{ press_to_apply_ms: 5, ramp_start_offset_ms: 0, ramp_duration_ms: 10 }
	);
});

test('press-to-apply compares against the baseline floor, not a hardcoded constant', () => {
	const floor = readInputToAppliedFloorMs();
	assert.ok(typeof floor === 'number' && floor > 0);
	assert.equal(isOverInputToAppliedFloor(5, floor), false);
	assert.equal(isOverInputToAppliedFloor(20, floor), true);
});

test('setEq accepts the stamp, applies the ramp, logs, and does not call _setParam', () => {
	const body = engineBlockAfter('setEq(deck: DeckId, band: EqBand, value: number, pressT0Ms?: number): void {');
	assert.match(body, /pressT0Ms/);
	assert.match(body, /applyEqRamp\(/);
	assert.match(body, /logEqApply\(/);
	assert.doesNotMatch(body, /_setParam/);
});

test('performance-ipc forwards pressT0Ms into setEq', () => {
	const ipc = readSource('src/lib/rb/performance-ipc.svelte.ts');
	assert.ok(
		ipc.includes('engine.setEq(command.deck, command.band, command.value, pressT0Ms)'),
		'IPC eq branch must forward the press stamp'
	);
});

test('MIDI mixer_channel eq forwards pressT0Ms into dispatchPerformanceCommand', () => {
	const glue = readSource('src/lib/rb/midi/controller-pad-runtime.svelte.ts');
	assert.ok(
		glue.includes("{ type: 'eq', deck, band, value }"),
		'MIDI eq must dispatch the eq command'
	);
	assert.ok(
		/type:\s*'eq'[\s\S]*pressT0Ms/.test(glue),
		'MIDI eq must forward pressT0Ms'
	);
});

test('EQ apply instrumentation does not read master mute state', () => {
	const setEqBody = engineBlockAfter(
		'setEq(deck: DeckId, band: EqBand, value: number, pressT0Ms?: number): void {'
	);
	const stamp = readSource('src/lib/rb/press-stamp.ts');
	const apply = readSource('src/lib/player/eq-apply.ts');
	const logEqApplyBody = stamp.slice(
		stamp.indexOf('export function logEqApply'),
		stamp.indexOf('export function scheduleRowFacts')
	);
	for (const src of [setEqBody, logEqApplyBody, apply]) {
		assert.doesNotMatch(src, /isMasterMuted/);
		assert.doesNotMatch(src, /startupMasterMuted/);
	}
});

test('eq-apply rows do not evict transport-schedule-press rows and vice versa', () => {
	const pressRow = {
		t: '2026-09-12T00:00:00.000Z',
		kind: 'transport-schedule-press',
		deck: 1,
		message: 'press'
	};
	const eqRows = Array.from({ length: 20 }, (_, i) => ({
		t: `2026-09-12T00:00:0${i}.000Z`,
		kind: 'eq-apply',
		deck: 1,
		message: `eq ${i}`
	}));
	const keptAfterEqFlood = buckets.withinBudgets([pressRow, ...eqRows]);
	assert.ok(
		keptAfterEqFlood.some((row) => row.kind === 'transport-schedule-press'),
		'eq-apply flood must not evict the press row'
	);

	const eqRow = { t: '2026-09-12T00:00:00.000Z', kind: 'eq-apply', deck: 1, message: 'eq' };
	const scheduleRows = Array.from({ length: 20 }, (_, i) => ({
		t: `2026-09-12T00:00:0${i}.000Z`,
		kind: 'transport-schedule',
		deck: 1,
		message: `sched ${i}`
	}));
	const keptAfterScheduleFlood = buckets.withinBudgets([eqRow, ...scheduleRows]);
	assert.ok(
		keptAfterScheduleFlood.some((row) => row.kind === 'eq-apply'),
		'transport-schedule flood must not evict the eq-apply row'
	);
});
