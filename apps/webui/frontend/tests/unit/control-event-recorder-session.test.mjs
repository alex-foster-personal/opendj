// [if] the recorder status poll re-binds the same session or the daemon reports a seq gap [then] seq stays valid, and samples count from the master tap origin, [else stop].
import assert from 'node:assert/strict';
import test from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const recorder = await loadTypeScriptModule('src/lib/sets/control-event-recorder.ts');
const origin = await loadTypeScriptModule('src/lib/sets/master-mix-origin.ts');

const stamp = { sample: 48_000, sample_rate: 48_000 };
const mute = { type: 'stem_mute', deck: 1, stem: 'vocal', muted: true };

/** Install a recorder seam that records posted seqs, failing through `fail` when it returns an error. */
function seam(fail = () => null) {
	const posted = [];
	const restore = recorder.installControlEventRecorderForTest({
		sampleClock: () => stamp,
		wallClock: () => '2026-11-13T12:00:00Z',
		postEnabled: true,
		post: async (events) => {
			const error = fail(events);
			if (error !== null) throw error;
			posted.push(...events.map((event) => event.seq));
		}
	});
	return { posted, restore };
}

test('re-binding the same session keeps the seq counter', async () => {
	const { posted, restore } = seam();
	recorder.bindControlEventSession('sess');
	await recorder.captureStemCommand(mute, 'user-ui', 0);
	recorder.bindControlEventSession('sess');
	await recorder.captureStemCommand({ ...mute, muted: false }, 'user-ui', 1000);
	assert.deepEqual(posted, [0, 1]);
	restore();
	recorder.resetControlEventRecorderForTest();
});

test('binding a different session restarts seq at zero', async () => {
	const { posted, restore } = seam();
	recorder.bindControlEventSession('a');
	await recorder.captureStemCommand(mute, 'user-ui', 0);
	recorder.bindControlEventSession('b');
	await recorder.captureStemCommand(mute, 'user-ui', 1000);
	assert.deepEqual(posted, [0, 0]);
	restore();
	recorder.resetControlEventRecorderForTest();
});

test('a daemon seq gap renumbers from the expected seq and retries', async () => {
	let expected = 7;
	const { posted, restore } = seam((events) => {
		if (events[0].seq === expected) return null;
		const error = new Error(`control event seq gap: expected ${expected}, got ${events[0].seq}`);
		error.status = 422;
		return error;
	});
	recorder.bindControlEventSession('sess');
	await recorder.captureStemCommand(mute, 'user-ui', 0);
	expected = 8;
	await recorder.captureStemCommand({ ...mute, muted: false }, 'user-ui', 1000);
	assert.deepEqual(posted, [7, 8]);
	restore();
	recorder.resetControlEventRecorderForTest();
});

test('expectedSeqFromError ignores other errors', () => {
	const plain = new Error('control event seq gap: expected 3, got 0');
	assert.equal(recorder.expectedSeqFromError(plain), null);
	const other = Object.assign(new Error('bad session'), { status: 422 });
	assert.equal(recorder.expectedSeqFromError(other), null);
});

test('samples count from the tap origin, not context creation', () => {
	const tap = {
		sessionId: 's',
		context: {},
		contextFrame: 600 * 48_000,
		recordedFrames: 0,
		sampleRate: 48_000
	};
	assert.equal(origin.recordedSampleAt(tap, 601), 48_000);
	assert.equal(origin.recordedSampleAt(tap, 599), null);
	const rebuilt = { ...tap, contextFrame: 0, recordedFrames: 96_000 };
	assert.equal(origin.recordedSampleAt(rebuilt, 1), 144_000);
});

test('origin clears only for its own session', () => {
	const tap = {
		sessionId: 's',
		context: {},
		contextFrame: 0,
		recordedFrames: 0,
		sampleRate: 48_000
	};
	origin.setMasterMixOrigin(tap);
	origin.clearMasterMixOrigin('other');
	assert.equal(origin.masterMixOrigin(), tap);
	origin.clearMasterMixOrigin('s');
	assert.equal(origin.masterMixOrigin(), null);
});
