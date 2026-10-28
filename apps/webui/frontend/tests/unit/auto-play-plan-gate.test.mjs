/**
 * AGENT-29: AutoPlay yields to an agent plan that is scheduled or running. [if] the plan runner reports 'scheduled' or
 * 'running' [then] AutoPlay makes no promotion and no handoff, [else] (done, aborted, failed, no plan) it runs as before.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const gate = await loadTypeScriptModule('src/lib/rb/auto-play-plan-gate.ts');


test('AutoPlay yields to a scheduled or running agent plan (AGENT-29)', () => {
	assert.equal(gate.autoPlayYieldsToPlan('scheduled'), true);
	assert.equal(gate.autoPlayYieldsToPlan('running'), true);
});

test('AutoPlay runs as before when no plan is active (AGENT-29)', () => {
	for (const status of ['done', 'aborted', 'failed', null, undefined]) {
		assert.equal(gate.autoPlayYieldsToPlan(status), false, `status ${String(status)}`);
	}
});

test('AutoPlay stands aside only while the registered plan runner has a plan scheduled or running (AGENT-29)', () => {
	assert.equal(gate.autoPlayYieldsNow(), false, 'no runner registered: no plan can be running');
	let status = 'running';
	gate.registerAgentPlanStatus(() => status);
	assert.equal(gate.autoPlayYieldsNow(), true);
	status = 'done';
	assert.equal(gate.autoPlayYieldsNow(), false);
});

test('AutoPlay reports a handoff in flight only through the reader it registered', () => {
	assert.equal(gate.autoPlayHandoffInFlight(), false, 'nothing registered: no handoff can be in flight');
	gate.registerAutoPlayHandoffInFlight(() => true);
	assert.equal(gate.autoPlayHandoffInFlight(), true);
	gate.registerAutoPlayHandoffInFlight(() => false);
	assert.equal(gate.autoPlayHandoffInFlight(), false);
});
