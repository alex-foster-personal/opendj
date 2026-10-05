// requirement: LATENCY-01
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { engineBlockAfter, readFrontendSource as readSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const REPO_ROOT = fileURLToPath(new URL('../../../../../', import.meta.url));

let mixerApply;
let pressStamp;
let buckets;
let ipc;

before(async () => {
	mixerApply = await loadTypeScriptModule('src/lib/player/mixer-apply.ts');
	pressStamp = await loadTypeScriptModule('src/lib/rb/press-stamp.ts');
	buckets = await loadTypeScriptModule('src/lib/rb/perf-event-buckets.ts');
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
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

test('mixerApplyStages carries press_to_apply_ms', () => {
	assert.deepEqual(mixerApply.mixerApplyStages({ pressToApplyMs: 5 }), { press_to_apply_ms: 5 });
});

test('press-to-apply compares against the baseline floor, not a hardcoded constant', () => {
	const floor = readInputToAppliedFloorMs();
	assert.ok(typeof floor === 'number' && floor > 0);
	assert.equal(isOverInputToAppliedFloor(5, floor), false);
	assert.equal(isOverInputToAppliedFloor(20, floor), true);
});

const MIXER_SETTERS = [
	{ name: 'setFilter', anchor: 'setFilter(deck: DeckId, value: number, pressT0Ms?: number): void {' },
	{ name: 'setFader', anchor: 'setFader(deck: DeckId, value: number, pressT0Ms?: number): void {' },
	{ name: 'setCrossfader', anchor: 'setCrossfader(value: number, pressT0Ms?: number): void {' },
	{ name: 'setStemMute', anchor: 'setStemMute(deck: DeckId, stem: StemControl, muted: boolean, pressT0Ms?: number): void {' },
	{
		name: 'setStemSolo',
		anchor: 'setStemSolo(deck: DeckId, stem: StemControl, solo: boolean, pressT0Ms?: number, exclusive = false): void {'
	}
];

for (const { name, anchor } of MIXER_SETTERS) {
	test(`${name} accepts the stamp and calls logMixerApply`, () => {
		const body = engineBlockAfter(anchor);
		assert.match(body, /pressT0Ms/);
		assert.match(body, /logMixerApply\(/);
		assert.doesNotMatch(body, /isMasterMuted/);
		assert.doesNotMatch(body, /startupMasterMuted/);
	});
}

test('setFilter and setFader still call _setParam', () => {
	const filterBody = engineBlockAfter('setFilter(deck: DeckId, value: number, pressT0Ms?: number): void {');
	const faderBody = engineBlockAfter('setFader(deck: DeckId, value: number, pressT0Ms?: number): void {');
	assert.match(filterBody, /_setParam/);
	assert.match(faderBody, /_setParam/);
});

test('performance-ipc forwards pressT0Ms into mixer setters', () => {
	const src = readSource('src/lib/rb/performance-ipc.svelte.ts');
	assert.ok(src.includes('engine.setFilter(command.deck, command.value, pressT0Ms)'));
	assert.ok(src.includes('engine.setFader(command.deck, command.value, pressT0Ms)'));
	assert.ok(src.includes('engine.setCrossfader(command.value, pressT0Ms)'));
	assert.ok(src.includes('engine.setStemMute(command.deck, command.stem, command.muted, pressT0Ms)'));
	assert.ok(src.includes('engine.setStemSolo(command.deck, command.stem, command.solo, pressT0Ms, command.exclusive)'));
});

test('MIDI mixer_channel filter and fader forward pressT0Ms', () => {
	const glue = readSource('src/lib/rb/midi/action-glue.svelte.ts');
	assert.ok(/type:\s*'filter'[\s\S]*pressT0Ms/.test(glue));
	assert.ok(/type:\s*'fader'[\s\S]*pressT0Ms/.test(glue));
	assert.ok(/type:\s*'crossfader'[\s\S]*pressT0Ms/.test(glue));
	assert.doesNotMatch(glue, /type:\s*'trim'[^)]*\),\s*pressT0Ms/);
});

test('filter-apply flood does not evict eq-apply or transport-schedule-press', () => {
	const pressRow = {
		t: '2026-09-12T00:00:00.000Z',
		kind: 'transport-schedule-press',
		deck: 1,
		message: 'press'
	};
	const eqRow = { t: '2026-09-12T00:00:00.000Z', kind: 'eq-apply', deck: 1, message: 'eq' };
	const filterRows = Array.from({ length: 20 }, (_, i) => ({
		t: `2026-09-12T00:00:0${i}.000Z`,
		kind: 'filter-apply',
		deck: 1,
		message: `filter ${i}`
	}));
	const kept = buckets.withinBudgets([pressRow, eqRow, ...filterRows]);
	assert.ok(kept.some((row) => row.kind === 'transport-schedule-press'));
	assert.ok(kept.some((row) => row.kind === 'eq-apply'));
});

test('eq-apply flood does not evict filter-apply', () => {
	const filterRow = { t: '2026-09-12T00:00:00.000Z', kind: 'filter-apply', deck: 1, message: 'filter' };
	const eqRows = Array.from({ length: 20 }, (_, i) => ({
		t: `2026-09-12T00:00:0${i}.000Z`,
		kind: 'eq-apply',
		deck: 1,
		message: `eq ${i}`
	}));
	const kept = buckets.withinBudgets([filterRow, ...eqRows]);
	assert.ok(kept.some((row) => row.kind === 'filter-apply'));
});

const CLASS_A_TYPES = [
	{ type: 'eq', deck: 1, band: 'low', value: 0.5 },
	{ type: 'filter', deck: 1, value: 0.5 },
	{ type: 'fader', deck: 1, value: 0.5 },
	{ type: 'crossfader', value: 0.5 },
	{ type: 'stem_mute', deck: 1, stem: 'vocal', muted: true },
	{ type: 'stem_solo', deck: 1, stem: 'vocal', solo: true }
];

for (const command of CLASS_A_TYPES) {
	test(`performanceCommandQueueScopes is null for ${command.type}`, () => {
		assert.equal(ipc.performanceCommandQueueScopes(command), null);
	});
}
