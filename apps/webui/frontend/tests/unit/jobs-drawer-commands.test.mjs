/**
 * PERFDRAWER-12580: the jobs drawer opens and closes through the ordinary
 * PerformanceCommand dispatcher, the way cmdk_open/cmdk_close and
 * track_info_open/track_info_close do, and its open state is part of
 * queryPerformanceState().
 *
 * - if an agent's drawer_open/drawer_close does not move the same state the
 *   JOBS button moves then broken
 * - if the drawer is missing from the performance state query then broken
 * - if an unknown drawer name or an extra field is accepted then broken
 * - if a second drawer API is hung off window.musicDjToolsPerformance then broken
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let uninstall;

before(async () => {
	mod = await loadTypeScriptModule('tests/unit/fixtures/jobs-drawer-commands-entry.ts');
	globalThis.window = {};
	uninstall = mod.installPerformanceBrowserIpc();
});

after(() => {
	uninstall();
	delete globalThis.window;
});

beforeEach(async () => {
	await mod.dispatchPerformanceCommand({ type: 'drawer_close', drawer: 'jobs' });
});

// requirement: PERFDRAWER-12580
test('an agent opens and closes the jobs drawer and the state query reports it', async () => {
	await mod.dispatchPerformanceCommand({ type: 'drawer_open', drawer: 'jobs' });
	assert.equal(mod.jobsStore.drawerOpen, true, 'the drawer the JOBS button shows is open');
	const state = mod.queryPerformanceState();
	assert.deepEqual(state.drawers, { jobs: { open: true } });
	assert.deepEqual(structuredClone(state.drawers), state.drawers, 'query is serializable');
	await mod.dispatchPerformanceCommand({ type: 'drawer_close', drawer: 'jobs' });
	assert.equal(mod.jobsStore.drawerOpen, false);
	assert.deepEqual(mod.queryPerformanceState().drawers, { jobs: { open: false } });
});

// requirement: PERFDRAWER-12580
test('the JOBS button toggle goes through the same dispatcher', async () => {
	assert.deepEqual(mod.jobsDrawerToggleCommand(), { type: 'drawer_open', drawer: 'jobs' });
	assert.deepEqual(await mod.runPerformanceCommandFromUi(mod.jobsDrawerToggleCommand()), {
		ok: true
	});
	assert.equal(mod.queryPerformanceState().drawers.jobs.open, true);
	assert.deepEqual(mod.jobsDrawerToggleCommand(), { type: 'drawer_close', drawer: 'jobs' });
	await mod.runPerformanceCommandFromUi(mod.jobsDrawerToggleCommand());
	assert.equal(mod.queryPerformanceState().drawers.jobs.open, false);
});

// requirement: PERFDRAWER-12580
test('an unknown drawer or extra field is refused and changes nothing', async () => {
	await mod.dispatchPerformanceCommand({ type: 'drawer_open', drawer: 'jobs' });
	await assert.rejects(
		mod.dispatchPerformanceCommand({ type: 'drawer_close', drawer: 'nope' }),
		/drawer must be one of jobs/
	);
	await assert.rejects(
		mod.dispatchPerformanceCommand({ type: 'drawer_close', drawer: 'jobs', extra: 1 }),
		/unexpected fields/
	);
	assert.equal(mod.queryPerformanceState().drawers.jobs.open, true);
});

// requirement: PERFDRAWER-14820
test('drawer commands skip the deck queue and there is no side drawer API', () => {
	assert.equal(mod.performanceCommandQueueScopes({ type: 'drawer_open', drawer: 'jobs' }), null);
	assert.equal(mod.performanceCommandQueueScopes({ type: 'drawer_close', drawer: 'jobs' }), null);
	assert.equal(globalThis.window.musicDjToolsPerformance.drawers, undefined);
});
