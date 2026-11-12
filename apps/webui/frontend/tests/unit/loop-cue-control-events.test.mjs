// [if] hot-cue presses [then] v2 rows use deck.N.hot_cue.* with sample clock, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const loopCue = await loadTypeScriptModule('src/lib/sets/loop-cue-control-events.ts');
const ids = await loadTypeScriptModule('src/lib/sets/control_ids.ts');
const recorder = await loadTypeScriptModule('src/lib/sets/control-event-recorder.ts');

const stamp = { sample: 96_000, sample_rate: 48_000 };

test('eventsFromHotCueTrigger returns one trigger row', () => {
	const rows = loopCue.eventsFromHotCueTrigger(
		{ deck: 1, slot: 'C' },
		2200.5,
		'user-ui',
		stamp,
		'sess',
		'2026-11-10T12:00:00Z'
	);
	assert.equal(rows.length, 1);
	assert.equal(rows[0].control, ids.hotCueId(1, 'C'));
	assert.equal(rows[0].value, 2200.5);
	assert.equal(rows[0].kind, 'trigger');
	assert.equal(rows[0].sample, stamp.sample);
	assert.equal(rows[0].sample_rate, stamp.sample_rate);
});

test('agent hot-cue maps to source agent', () => {
	const rows = loopCue.eventsFromHotCueTrigger(
		{ deck: 2, slot: 'A' },
		500.0,
		'agent-command',
		stamp,
		'sess',
		'2026-11-10T12:00:00Z'
	);
	assert.equal(rows[0].source, 'agent');
});

test('two hot-cue presses at same time post seq 0 and 1', async () => {
	const posts = [];
	const restore = recorder.installControlEventRecorderForTest({
		sampleClock: () => stamp,
		wallClock: () => '2026-11-10T12:00:00Z',
		post: async (events) => {
			posts.push(...events);
		}
	});
	recorder.bindControlEventSession('sess');
	const now = 1000;
	await recorder.captureHotCueTrigger({ deck: 1, slot: 'A' }, 100.0, 'user-ui', now);
	await recorder.captureHotCueTrigger({ deck: 1, slot: 'B' }, 200.0, 'user-ui', now);
	assert.equal(posts.length, 2);
	assert.deepEqual(
		posts.map((r) => r.seq),
		[0, 1]
	);
	restore();
	recorder.resetControlEventRecorderForTest();
});

test('post chain keeps seq order when first post resolves late', async () => {
	const posts = [];
	let releaseFirst;
	const firstGate = new Promise((resolve) => {
		releaseFirst = resolve;
	});
	let call = 0;
	const restore = recorder.installControlEventRecorderForTest({
		sampleClock: () => stamp,
		wallClock: () => '2026-11-10T12:00:00Z',
		post: async (events) => {
			call += 1;
			if (call === 1) {
				await firstGate;
			}
			posts.push(...events);
		}
	});
	recorder.bindControlEventSession('sess');
	const p1 = recorder.captureHotCueTrigger({ deck: 1, slot: 'A' }, 100.0, 'user-ui');
	const p2 = recorder.captureHotCueTrigger({ deck: 1, slot: 'B' }, 200.0, 'user-ui');
	releaseFirst();
	await Promise.all([p1, p2]);
	assert.deepEqual(
		posts.map((r) => r.seq),
		[0, 1]
	);
	restore();
	recorder.resetControlEventRecorderForTest();
});

test('capture skips when sample clock is unavailable', async () => {
	const posts = [];
	const restore = recorder.installControlEventRecorderForTest({
		sampleClock: () => null,
		post: async (events) => {
			posts.push(...events);
		}
	});
	recorder.bindControlEventSession('sess');
	await recorder.captureHotCueTrigger({ deck: 1, slot: 'A' }, 100.0, 'user-ui');
	assert.equal(posts.length, 0);
	restore();
	recorder.resetControlEventRecorderForTest();
});

test('performance-ipc calls captureHotCueTrigger before planHotCueTrigger', () => {
	const src = readFileSync('src/lib/rb/performance-ipc.svelte.ts', 'utf8');
	const branch = src.slice(src.indexOf("command.type === 'hot_cue_trigger'"));
	assert.match(branch, /captureHotCueTrigger\(/);
	const capIdx = branch.indexOf('captureHotCueTrigger(');
	const planIdx = branch.indexOf('planHotCueTrigger(');
	assert.ok(capIdx >= 0 && planIdx > capIdx);
});
