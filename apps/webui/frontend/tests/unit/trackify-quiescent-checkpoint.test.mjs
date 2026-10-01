/**
 * PERFMODE-15 leak capture quiescent checkpoints
 * (`scripts/perf/trackify-quiescent-checkpoint.mjs`,
 * ADR-NEW-trackify-leak-kpi-quiescent-baselines).
 *
 * A baseline is only a retention reading if the deck is empty and garbage has
 * been collected when it is sampled, and the protocol must hand control back
 * to playback after every checkpoint.
 */
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { test } from 'node:test';

import {
	createLineReader,
	quiesceTrackify,
	resumeTrackify,
	runLeakProtocol
} from '../../../../../scripts/perf/trackify-quiescent-checkpoint.mjs';

const NO_WAIT = { settleMs: 0, pressureSettleMs: 0 };

/**
 * A page over a fake Trackify session. `reloadsWhileQuiescent` models a build
 * whose autoplay ignores "off": the deck loads the next track during the
 * settle wait, after the unload has already been observed.
 */
function fakeTrackifyPage({ reloadsWhileQuiescent = false } = {}) {
	const log = [];
	const state = { autoplay: true, stable_id: 'track-1', playing: true, loads: 1 };
	const loadNext = () => {
		state.loads += 1;
		state.stable_id = `track-${state.loads}`;
		state.playing = true;
		log.push(`load ${state.stable_id}`);
	};
	const window = {
		musicDjToolsTrackify: {
			query: () => ({ deck: { stable_id: state.stable_id, playing: state.playing } }),
			toggle_autoplay: (enabled) => {
				state.autoplay = enabled;
				log.push(`autoplay ${enabled}`);
				if (enabled && state.stable_id === null) loadNext();
			}
		},
		musicDjToolsPerformance: {
			dispatch: (command) => {
				log.push(`dispatch ${command.type} ${command.deck}`);
				if (command.type === 'unload') {
					state.stable_id = null;
					state.playing = false;
				}
			}
		}
	};
	const run = (fn, arg) => {
		globalThis.window = window;
		try {
			return fn(arg);
		} finally {
			delete globalThis.window;
		}
	};
	return {
		log,
		state,
		evaluate: async (fn, arg) => run(fn, arg),
		waitForFunction: async (fn, arg) => {
			for (let i = 0; i < 5; i += 1) {
				const value = run(fn, arg);
				if (value) return value;
			}
			throw new Error('fake waitForFunction never became truthy');
		},
		waitForTimeout: async () => {
			log.push(`deck ${state.stable_id ?? 'empty'} at settle`);
			if (reloadsWhileQuiescent && state.stable_id === null) loadNext();
		},
		send: async (method, params) => {
			log.push(`${method}${params?.level ? ` ${params.level}` : ''} deck=${state.stable_id ?? 'empty'}`);
			return {};
		}
	};
}

test('quiescing unloads the deck before it collects garbage and sends critical memory pressure', async () => {
	const page = fakeTrackifyPage();
	await quiesceTrackify(page, NO_WAIT);
	assert.deepEqual(page.log, [
		'autoplay false',
		'dispatch unload 1',
		'deck empty at settle',
		'HeapProfiler.collectGarbage deck=empty',
		'HeapProfiler.collectGarbage deck=empty',
		'Memory.simulatePressureNotification critical deck=empty',
		'deck empty at settle',
		'HeapProfiler.collectGarbage deck=empty',
		'HeapProfiler.collectGarbage deck=empty'
	]);
	assert.equal(page.state.autoplay, false);
});

test('a deck that reloads while quiescent invalidates the baseline instead of sampling it', async () => {
	const page = fakeTrackifyPage({ reloadsWhileQuiescent: true });
	await assert.rejects(quiesceTrackify(page, NO_WAIT), /quiescent checkpoint invalid: deck 1 reloaded track-2/);
});

test('resuming turns autoplay back on and waits for a playing track', async () => {
	const page = fakeTrackifyPage();
	await quiesceTrackify(page, NO_WAIT);
	await resumeTrackify(page);
	assert.equal(page.state.autoplay, true);
	assert.equal(page.state.stable_id, 'track-2');
	assert.equal(page.state.playing, true);
});

function scriptedLines(lines) {
	const queue = [...lines];
	return () => {
		if (queue.length === 0) return Promise.reject(new Error('no more protocol lines'));
		return Promise.resolve(queue.shift());
	};
}

test('the protocol answers each CHECKPOINT with QUIESCENT, each RESUME with RESUMED, and watches every segment', async () => {
	const page = fakeTrackifyPage();
	const emitted = [];
	const watched = [];
	await runLeakProtocol(
		page,
		scriptedLines(['CHECKPOINT', 'RESUME', 'CHECKPOINT', 'RESUME', 'NEXT']),
		(line) => emitted.push(`${line} deck=${page.state.stable_id ?? 'empty'}`),
		async (_page, line) => watched.push(await line),
		NO_WAIT
	);
	assert.deepEqual(emitted, [
		'QUIESCENT deck=empty',
		'RESUMED deck=track-2',
		'QUIESCENT deck=empty',
		'RESUMED deck=track-3'
	]);
	assert.deepEqual(watched, ['CHECKPOINT', 'CHECKPOINT', 'NEXT']);
});

test('the protocol refuses an unknown command and a missing RESUME', async () => {
	const watch = async (_page, line) => line;
	await assert.rejects(
		runLeakProtocol(fakeTrackifyPage(), scriptedLines(['SAMPLE']), () => {}, watch, NO_WAIT),
		/expected CHECKPOINT or NEXT, got "SAMPLE"/
	);
	await assert.rejects(
		runLeakProtocol(fakeTrackifyPage(), scriptedLines(['CHECKPOINT', 'NEXT']), () => {}, watch, NO_WAIT),
		/expected RESUME after QUIESCENT, got "NEXT"/
	);
});

test('the line reader yields lines in order, including ones that arrived before they were asked for', async () => {
	const input = new PassThrough();
	const reader = createLineReader(input);
	input.write('CHECKPOINT\nRESUME\n');
	assert.equal(await reader.next(), 'CHECKPOINT');
	assert.equal(await reader.next(), 'RESUME');
	const pending = reader.next();
	input.write('NEXT\n');
	assert.equal(await pending, 'NEXT');
	input.end();
	await assert.rejects(reader.next(), /stdin closed before the next protocol line/);
});
