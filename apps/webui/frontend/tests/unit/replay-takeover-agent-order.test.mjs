/**
 * SETREC-12067: replay_takeover side order on the agent command bus (issue #10736).
 *
 * [if] replay_takeover all is posted while replay runs [then] the step succeeds with taken_over all
 * [if] an unknown control id is posted [then] the step fails with unknown control id
 * [if] control is not a string or extra keys are present [then] the step fails before take-over
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STUB = fileURLToPath(new URL('./fixtures/set-replay-ipc-stub.ts', import.meta.url));

const entry = await loadTypeScriptModule('tests/unit/fixtures/set-replay-agent-orders-entry.ts', {
	alias: {
		'$lib/rb/performance-ipc.svelte': STUB
	}
});

afterEach(() => {
	entry.stopReplay();
});

test('agent replay_takeover all succeeds when replay is active', async () => {
	entry.startReplay();
	const out = await entry.executeAgentOrder({
		kind: 'single',
		payload: { type: 'replay_takeover', control: 'all' }
	});
	assert.equal(out.steps.length, 1);
	assert.equal(out.steps[0].status, 'succeeded');
	assert.equal(out.steps[0].result.taken_over, 'all');
});

test('agent replay_takeover refuses unknown control id', async () => {
	entry.startReplay();
	const bad = await entry.executeAgentOrder({
		kind: 'single',
		payload: { type: 'replay_takeover', control: 'deck.9.x' }
	});
	assert.equal(bad.steps[0].status, 'failed');
	assert.match(bad.steps[0].error, /unknown control id/);
});

test('agent replay_takeover validates payload shape', async () => {
	entry.startReplay();
	const notString = await entry.executeAgentOrder({
		kind: 'single',
		payload: { type: 'replay_takeover', control: 1 }
	});
	assert.equal(notString.steps[0].status, 'failed');
	assert.match(notString.steps[0].error, /requires a string control/);
	const extra = await entry.executeAgentOrder({
		kind: 'single',
		payload: { type: 'replay_takeover', control: 'all', deck: 1 }
	});
	assert.equal(extra.steps[0].status, 'failed');
	assert.match(extra.steps[0].error, /allows only type and control/);
});
