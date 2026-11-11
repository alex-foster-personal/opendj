/**
 * SETREC-12067 replay take-over (issue #10736).
 *
 * [if] a control is touched while replay runs [then] the next replay dispatch for it is dropped
 *   with no await in between [then broken if a replay value applies after the touch].
 * [if] take over all runs [then] zero replay dispatches follow for any control [then broken if one does].
 * [if] a live move follows take-over [then] it dispatches with its own source, never replay [then broken if tagged replay].
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const STUB = fileURLToPath(new URL('./fixtures/set-replay-ipc-stub.ts', import.meta.url));
const CONTROL_IDS = fileURLToPath(
	new URL('../../../../../apps/sets/control_ids.py', import.meta.url)
);

const mod = await loadTypeScriptModule('tests/unit/fixtures/set-replay-test-harness.ts', {
	alias: {
		'$lib/rb/performance-ipc.svelte': STUB
	}
});

const {
	replayTakeover,
	replayDispatchGate,
	replayDispatchTakenOver,
	v2EventSource,
	endReplayTakeoverSession,
	startReplay,
	stopReplay,
	dispatchReplayEvent,
	replayTakeOver,
	replayControlId,
	REPLAY_CONTROL_IDS,
	dispatchRecords,
	dispatchPerformanceCommand
} = mod;

function countReplay(control) {
	return dispatchRecords.filter((r) => r.control === control && r.source === 'replay').length;
}

function commandForId(id) {
	if (id === 'mixer.crossfader') return { type: 'crossfader', value: 0.5 };
	if (id === 'mixer.master_volume') return { type: 'master_volume', value: 0.8 };
	if (id === 'mixer.master_mute') return { type: 'master_mute', muted: false };
	const m = /^deck\.(\d+)\.eq\.(low|mid|high)$/.exec(id);
	if (m) return { type: 'eq', deck: Number(m[1]), band: m[2], value: 0.5 };
	const deck = /^deck\.(\d+)\.(.+)$/.exec(id);
	if (!deck) throw new Error(`no command for ${id}`);
	const n = Number(deck[1]);
	const tail = deck[2];
	if (tail === 'trim') return { type: 'trim', deck: n, value: 0.5 };
	if (tail === 'auto_gain')
		return { type: 'auto_gain_offset', deck: n, offset_db: 0, stable_id: 't' };
	if (tail === 'filter') return { type: 'filter', deck: n, value: 0.5 };
	if (tail === 'fader') return { type: 'fader', deck: n, value: 0.5 };
	if (tail === 'assign') return { type: 'assign', deck: n, assign: 'THRU' };
	if (tail === 'tempo') return { type: 'tempo', deck: n, bpm: 120 };
	const stem = /^stem\.(.+)\.gain$/.exec(tail);
	if (stem) return { type: 'stem_gain', deck: n, stem: stem[1], value: 0.5 };
	throw new Error(`no command for ${id}`);
}

beforeEach(() => {
	dispatchRecords.length = 0;
	stopReplay();
});

test('per-control take-over drops replay within one turn', async () => {
	startReplay();
	await dispatchReplayEvent({ type: 'eq', deck: 1, band: 'low', value: 0.4 });
	await dispatchReplayEvent({ type: 'crossfader', value: 0.3 });
	assert.equal(countReplay('deck.1.eq.low'), 1);
	assert.equal(countReplay('mixer.crossfader'), 1);
	await dispatchPerformanceCommand(
		{ type: 'eq', deck: 1, band: 'low', value: 0.9 },
		undefined,
		'user-ui'
	);
	const beforeCross = countReplay('mixer.crossfader');
	const outcome = await dispatchReplayEvent({ type: 'eq', deck: 1, band: 'low', value: 0.2 });
	assert.equal(outcome, 'taken_over');
	assert.equal(countReplay('deck.1.eq.low'), 1);
	await dispatchReplayEvent({ type: 'crossfader', value: 0.6 });
	assert.equal(countReplay('mixer.crossfader'), beforeCross + 1);
});

test('take over all leaves no replay dispatch', async () => {
	startReplay();
	replayTakeOver('all', 'user');
	assert.equal(replayTakeover.taken_over_all, true);
	for (const id of REPLAY_CONTROL_IDS) {
		const outcome = await dispatchReplayEvent(commandForId(id));
		assert.equal(outcome, 'taken_over');
	}
	assert.equal(dispatchRecords.filter((r) => r.source === 'replay').length, 0);
});

test('live moves keep their real source after take-over', async () => {
	startReplay();
	await dispatchPerformanceCommand(
		{ type: 'eq', deck: 1, band: 'low', value: 0.5 },
		undefined,
		'user-ui'
	);
	await dispatchPerformanceCommand({ type: 'crossfader', value: 0.5 }, undefined, 'agent-command');
	assert.equal(replayTakeover.live['deck.1.eq.low'].source, 'user');
	assert.equal(replayTakeover.live['mixer.crossfader'].source, 'agent');
	assert.equal(v2EventSource('user-ui'), 'user');
	assert.equal(v2EventSource('agent-command'), 'agent');
	assert.equal(v2EventSource('replay'), 'replay');
	assert.equal(v2EventSource('app-command'), 'user');
	const live = dispatchRecords.filter((r) => r.source !== 'replay');
	assert.ok(live.every((r) => r.source === 'user-ui' || r.source === 'agent-command'));
});

test('execute-time re-check drops queued replay after touch', () => {
	startReplay();
	const cmd = { type: 'crossfader', value: 0.5 };
	assert.equal(replayDispatchTakenOver(cmd, 'replay'), false);
	replayDispatchGate({ type: 'crossfader', value: 0.2 }, 'user-ui');
	assert.equal(replayDispatchTakenOver(cmd, 'replay'), true);
});

test('errors when replay inactive or control unknown', () => {
	assert.equal(replayDispatchGate({ type: 'crossfader', value: 0.5 }, 'user-ui'), 'dispatch');
	assert.throws(() => replayTakeOver('all', 'user'), /no replay is running/);
	startReplay();
	assert.throws(() => replayTakeOver('deck.9.x', 'user'), /unknown control id/);
});

test('control ids match ENGINE_MIXER_CONTROL_IDS', () => {
	const py = readFileSync(CONTROL_IDS, 'utf8');
	const fromPy = new Set(
		py.match(/"(deck\.\d\.[a-z_.]+|mixer\.[a-z_]+)"/g).map((s) => s.slice(1, -1))
	);
	const fromTs = new Set();
	const types = [
		['trim', 1],
		['auto_gain_offset', 1],
		['filter', 1],
		['fader', 1],
		['assign', 1]
	];
	for (let deck = 1; deck <= 4; deck += 1) {
		for (const [type] of types) {
			if (type === 'trim') fromTs.add(replayControlId({ type: 'trim', deck, value: 0 }));
			if (type === 'auto_gain_offset')
				fromTs.add(
					replayControlId({ type: 'auto_gain_offset', deck, offset_db: 0, stable_id: 'x' })
				);
			if (type === 'filter') fromTs.add(replayControlId({ type: 'filter', deck, value: 0 }));
			if (type === 'fader') fromTs.add(replayControlId({ type: 'fader', deck, value: 0 }));
			if (type === 'assign') fromTs.add(replayControlId({ type: 'assign', deck, assign: 'THRU' }));
		}
		for (const band of ['low', 'mid', 'high']) {
			fromTs.add(replayControlId({ type: 'eq', deck, band, value: 0 }));
		}
	}
	fromTs.add(replayControlId({ type: 'crossfader', value: 0 }));
	fromTs.add(replayControlId({ type: 'master_volume', value: 0 }));
	fromTs.add(replayControlId({ type: 'master_mute', muted: false }));
	fromTs.delete(null);
	assert.deepEqual([...fromTs].sort(), [...fromPy].sort());
});

test('performance-ipc wires replay gate before dispatch', () => {
	const src = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url)),
		'utf8'
	);
	const dispatchBody = src
		.split('export async function dispatchPerformanceCommand')[1]
		.split('export async function')[0];
	assert.match(dispatchBody, /replayDispatchGate\(/);
	assert.ok(
		dispatchBody.indexOf('replayDispatchGate(') < dispatchBody.indexOf('_dispatchUnknown(')
	);
	const executeBody = src
		.split('function _executeAs')[1]
		.split('function _autoPlayOffNeedsUser')[0];
	assert.match(executeBody, /replayDispatchTakenOver\(/);
	assert.ok(
		executeBody.indexOf('replayDispatchTakenOver(') < executeBody.indexOf('withDeckCommandSource(')
	);
});

test('ReplayTakeoverBar SSR markup', async () => {
	const ssr = await loadSvelteSsrModule(
		[
			"export { default as ReplayTakeoverBar } from '$lib/components/rb/ReplayTakeoverBar.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n'),
		{}
	);
	const partial = ssr.render(ssr.ReplayTakeoverBar, {
		props: {
			live: [
				{ control: 'deck.1.eq.low', source: 'user', at_ms: 0 },
				{ control: 'mixer.crossfader', source: 'agent', at_ms: 1 }
			],
			takenOverAll: false,
			onTakeOverAll: () => {}
		}
	}).body;
	assert.match(partial, /data-replay-live="deck\.1\.eq\.low"/);
	assert.match(partial, /data-testid="replay-takeover-all"/);
	const all = ssr.render(ssr.ReplayTakeoverBar, {
		props: { live: [], takenOverAll: true, onTakeOverAll: () => {} }
	}).body;
	assert.match(all, /data-replay-taken-over-all="true"/);
	assert.doesNotMatch(all, /data-testid="replay-takeover-all"/);
});
