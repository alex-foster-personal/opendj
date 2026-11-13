/**
 * #12602 ((private path) section 5): the typed page command path keeps
 * who asked, so the page capture point (the control-event recorder) can tell
 * UI, agent and replay commands apart.
 *
 * [if] a UI command omits source [then] the recorded v2 row carries source user, [else stop].
 * [if] an agent order dispatches a supported command [then] the row carries source agent, [else stop].
 * [if] a caller supplies an unsupported source [then] the dispatcher rejects it before dispatch, [else stop].
 *
 * Seams: installControlEventRecorderForTest (sample clock, wall clock, post
 * sink) and resetControlEventRecorderForTest from control-event-recorder.ts.
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// The engine reads the page URL for ?extroute=; these tests run as a page with no query.
globalThis.location ??= { search: '' };

const entry = await loadTypeScriptModule('tests/unit/fixtures/command-source-entry.ts');

const STEM_GAIN = { type: 'stem_gain', deck: 1, stem: 'vocal', value: 0.4 };

let posted;
let restoreRecorder;
let uninstallIpc;

beforeEach(() => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	uninstallIpc = entry.installPerformanceBrowserIpc();
	posted = [];
	restoreRecorder = entry.installControlEventRecorderForTest({
		sampleClock: () => ({ sample: 96_000, sample_rate: 48_000 }),
		wallClock: () => '2026-11-13T12:00:00Z',
		post: async (events) => {
			posted.push(...events);
		}
	});
	entry.bindControlEventSession('sess-12602');
});

afterEach(() => {
	restoreRecorder();
	entry.resetControlEventRecorderForTest();
	uninstallIpc();
	delete globalThis.window;
});

/** The recorder posts without being awaited by the dispatcher; let it settle. */
async function recordedSources() {
	await new Promise((resolve) => setImmediate(resolve));
	return posted.map((row) => row.source);
}

test('a UI command that omits source dispatches as user', async () => {
	const result = await entry.runPerformanceCommandFromUi(STEM_GAIN);
	assert.deepEqual(result, { ok: true });
	assert.deepEqual(await recordedSources(), ['user']);
});

test('an agent order dispatches its command as agent', async () => {
	const order = await entry.executeAgentOrder({ kind: 'single', payload: STEM_GAIN });
	assert.equal(order.steps[0].status, 'succeeded');
	assert.deepEqual(await recordedSources(), ['agent']);
});

test('an unsupported source is rejected before dispatch', async () => {
	for (const source of ['user', 'robot', 42]) {
		await assert.rejects(
			() => entry.dispatchPerformanceCommand(STEM_GAIN, undefined, source),
			new RegExp(`source ${JSON.stringify(source)} is not supported; expected one of user-ui,`)
		);
	}
	assert.deepEqual(await recordedSources(), [], 'a refused command reaches no capture point');
	assert.deepEqual(
		[...entry.PAGE_COMMAND_SOURCES],
		['user-ui', 'agent-command', 'autoplay-handoff', 'app-command', 'engine', 'replay']
	);
});
