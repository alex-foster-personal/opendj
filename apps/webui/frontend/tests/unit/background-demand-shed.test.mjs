/**
 * PERFMODE-04 background demand shed: ordered deferral under pressure and xruns.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const GATE_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/playing-gate.ts', import.meta.url)),
	'utf8'
);

let gateModule;

before(async () => {
	gateModule = await loadTypeScriptModule('src/lib/rb/playing-gate.ts', {
		viteApiBase: 'https://playing-gate.example.test'
	});
});

function flush() {
	return new Promise((resolve) => setImmediate(resolve));
}

function makeShed({
	playing = false,
	pressureElevated = false,
	xruns = 0,
	jobs = [],
	notify
} = {}) {
	const state = {
		playing,
		pressureElevated,
		xruns,
		rows: [],
		notifications: [],
		runs: new Map()
	};
	for (const job of jobs) {
		state.runs.set(job.id, 0);
	}
	const wrappedJobs = jobs.map((job) => ({
		id: job.id,
		run: async () => {
			state.runs.set(job.id, (state.runs.get(job.id) ?? 0) + 1);
			await job.run?.();
		}
	}));
	const shed = gateModule.createBackgroundDemandShed({
		isPlaying: () => state.playing,
		pressureElevated: () => state.pressureElevated,
		readXruns: () => state.xruns,
		jobs: wrappedJobs,
		notify: notify ?? ((suggestion) => state.notifications.push(suggestion)),
		now: () => 1000,
		record: (kind, stages, deck, labels) => state.rows.push({ kind, stages, deck, labels })
	});
	return { shed, state };
}

test('playing with elevated kernel defers library-poll-cadence', () => {
	const { shed, state } = makeShed({
		playing: true,
		pressureElevated: true,
		jobs: [{ id: 'library-poll-cadence' }]
	});
	shed.request('library-poll-cadence');
	assert.equal(state.runs.get('library-poll-cadence'), 0);
	assert.equal(shed.pending, true);
});

test('playing with churnScore 500 defers work', () => {
	const { shed, state } = makeShed({
		playing: true,
		pressureElevated: true,
		jobs: [{ id: 'library-poll-cadence' }]
	});
	shed.request('library-poll-cadence');
	assert.equal(state.runs.get('library-poll-cadence'), 0);
	assert.equal(shed.pending, true);
});

test('playing with xrun window delta 1 defers work', () => {
	const { shed, state } = makeShed({
		playing: true,
		xruns: 4,
		jobs: [{ id: 'library-poll-cadence' }]
	});
	shed.sync();
	state.xruns = 5;
	shed.request('library-poll-cadence');
	assert.equal(state.runs.get('library-poll-cadence'), 0);
	assert.equal(shed.pending, true);
});

test('playing with normal signals runs immediately', async () => {
	const { shed, state } = makeShed({
		playing: true,
		jobs: [{ id: 'library-poll-cadence' }]
	});
	shed.request('library-poll-cadence');
	await flush();
	assert.equal(state.runs.get('library-poll-cadence'), 1);
	assert.equal(shed.pending, false);
});

test('not playing with elevated pressure runs immediately', async () => {
	const { shed, state } = makeShed({
		pressureElevated: true,
		jobs: [{ id: 'library-poll-cadence' }]
	});
	shed.request('library-poll-cadence');
	await flush();
	assert.equal(state.runs.get('library-poll-cadence'), 1);
});

test('missing pressure fields do not block xrun deferral', () => {
	const { shed, state } = makeShed({
		playing: true,
		xruns: 10,
		jobs: [{ id: 'library-poll-cadence' }]
	});
	shed.sync();
	state.xruns = 11;
	shed.request('library-poll-cadence');
	assert.equal(state.runs.get('library-poll-cadence'), 0);
	assert.equal(shed.pending, true);
});

test('a burst while elevated collapses to one run after pressure clears', async () => {
	const { shed, state } = makeShed({
		playing: true,
		pressureElevated: true,
		jobs: [{ id: 'library-poll-cadence' }]
	});
	for (let i = 0; i < 40; i++) shed.request('library-poll-cadence');
	state.pressureElevated = false;
	shed.sync();
	await flush();
	assert.equal(state.runs.get('library-poll-cadence'), 1);
	assert.equal(state.rows[0].stages.coalesced, 40);
	assert.equal(state.rows[0].labels.resumedBy, 'pressure-cleared');
});

test('two owed ids drain in list order', async () => {
	const order = [];
	const { shed, state } = makeShed({
		playing: true,
		pressureElevated: true,
		jobs: [
			{ id: 'library-poll-cadence', run: async () => order.push('library-poll-cadence') },
			{ id: 'eager-stem-decode', run: async () => order.push('eager-stem-decode') }
		]
	});
	shed.request('eager-stem-decode');
	shed.request('library-poll-cadence');
	state.pressureElevated = false;
	shed.sync();
	await flush();
	assert.deepEqual(order, ['library-poll-cadence', 'eager-stem-decode']);
});

test('sync while still elevated does not drain', async () => {
	const { shed, state } = makeShed({
		playing: true,
		pressureElevated: true,
		jobs: [{ id: 'library-poll-cadence' }]
	});
	shed.request('library-poll-cadence');
	shed.sync();
	await flush();
	assert.equal(state.runs.get('library-poll-cadence'), 0);
});

test('the shed re-arms after a drain episode', async () => {
	const { shed, state } = makeShed({
		playing: true,
		pressureElevated: true,
		jobs: [{ id: 'library-poll-cadence' }]
	});
	shed.request('library-poll-cadence');
	state.pressureElevated = false;
	shed.sync();
	await flush();
	assert.equal(state.runs.get('library-poll-cadence'), 1);

	state.pressureElevated = true;
	shed.request('library-poll-cadence');
	assert.equal(shed.pending, true);
	state.pressureElevated = false;
	shed.sync();
	await flush();
	assert.equal(state.runs.get('library-poll-cadence'), 2);
});

test('transition into elevated+playing fires toast suggestions once per episode', () => {
	const { shed, state } = makeShed({ playing: true });
	state.pressureElevated = true;
	shed.sync();
	assert.equal(state.notifications.length, 2);
	assert.ok(state.notifications.some((n) => n.message.includes('2-channel mode')));
	assert.ok(state.notifications.some((n) => n.message.includes('live-generation off')));
	shed.sync();
	assert.equal(state.notifications.length, 2, 'a second sync in the same episode does not fire again');
});

test('toast notify is the only side effect on elevation', () => {
	const { shed, state } = makeShed({
		playing: true,
		jobs: [{ id: 'eager-stem-decode' }]
	});
	state.pressureElevated = true;
	shed.sync();
	assert.equal(state.runs.get('eager-stem-decode'), 0);
});

test('P0 ids are not on BACKGROUND_SHED_JOBS', () => {
	for (const id of ['track-select', 'audio-callbacks', 'transport']) {
		assert.equal(gateModule.BACKGROUND_SHED_JOBS.includes(id), false);
		assert.equal(gateModule.P0_NEVER_SHED.includes(id), true);
	}
});

test('registering a P0 id in jobs throws', () => {
	assert.throws(
		() =>
			gateModule.createBackgroundDemandShed({
				isPlaying: () => false,
				pressureElevated: () => false,
				readXruns: () => 0,
				jobs: [{ id: 'track-select', run: async () => {} }]
			}),
		/P0 job track-select cannot be shed/
	);
});

test('BACKGROUND_SHED_JOBS ends with cloudsync-scheduler and not in P0', () => {
	const jobs = gateModule.BACKGROUND_SHED_JOBS;
	assert.equal(jobs.at(-1), 'cloudsync-scheduler');
	assert.equal(gateModule.P0_NEVER_SHED.includes('cloudsync-scheduler'), false);
});

test('elevated playing defers cloudsync-scheduler', () => {
	const { shed, state } = makeShed({
		playing: true,
		pressureElevated: true,
		jobs: [{ id: 'cloudsync-scheduler' }]
	});
	shed.request('cloudsync-scheduler');
	assert.equal(state.runs.get('cloudsync-scheduler'), 0);
	assert.equal(shed.pending, true);
});

test('pressure clear drains owed cloudsync job once', async () => {
	const { shed, state } = makeShed({
		playing: true,
		pressureElevated: true,
		jobs: [{ id: 'cloudsync-scheduler' }]
	});
	shed.request('cloudsync-scheduler');
	state.pressureElevated = false;
	shed.sync();
	await flush();
	assert.equal(state.runs.get('cloudsync-scheduler'), 1);
	assert.equal(shed.pending, false);
});

test('two owed ids including cloudsync drain in list order', async () => {
	const order = [];
	const { shed, state } = makeShed({
		playing: true,
		pressureElevated: true,
		jobs: [
			{ id: 'library-poll-cadence', run: async () => order.push('library-poll-cadence') },
			{ id: 'cloudsync-scheduler', run: async () => order.push('cloudsync-scheduler') }
		]
	});
	shed.request('cloudsync-scheduler');
	shed.request('library-poll-cadence');
	state.pressureElevated = false;
	shed.sync();
	await flush();
	assert.deepEqual(order, ['library-poll-cadence', 'cloudsync-scheduler']);
});

test('playing-gate shed source does not wire pushToast or P0 jobs', () => {
	assert.doesNotMatch(GATE_SOURCE, /pushToast/);
	assert.doesNotMatch(GATE_SOURCE, /channelCount\s*=/);
	assert.doesNotMatch(GATE_SOURCE, /liveGeneration\s*=/);
	assert.doesNotMatch(GATE_SOURCE, /liveStems\s*=/);
	const shedList = GATE_SOURCE.slice(
		GATE_SOURCE.indexOf('export const BACKGROUND_SHED_JOBS'),
		GATE_SOURCE.indexOf('export type BackgroundShedJobId')
	);
	assert.doesNotMatch(shedList, /track-select/);
	assert.doesNotMatch(shedList, /audio-callbacks/);
});
