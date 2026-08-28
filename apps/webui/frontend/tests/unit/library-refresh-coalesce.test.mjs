/**
 * C5/C3 - the background library refresh driven by the WS invalidation bus.
 *
 * BrowserPanel cannot be mounted in this node harness, so the coalescer it
 * depends on is tested directly (src/lib/rb/coalesce.ts) and the wiring that
 * makes BrowserPanel USE it is asserted against the component source, the same
 * way inert-controls.test.mjs asserts a rule that lives in markup.
 *
 * Regression lines:
 * - if coalesce lets two runs overlap then one library.changed frame costs two
 *   concurrent full refetches racing to write the same panes
 * - if coalesce drops the trailing run then an invalidation that landed
 *   mid-refetch is lost and the pane stays stale forever
 * - if a rejected run leaves the coalescer armed then every later refresh is
 *   wedged
 * - if BrowserPanel stops routing its bus subscriptions through the coalescer
 *   then C5 is back
 * - if _refreshLibraryRowsOnce stops rechecking playlist_id after its await
 *   then a playlist switch mid-fetch paints pane B with pane A's rows
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const BROWSER_PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);

let coalesceModule;

/** A run whose completion the test controls, so overlap is deterministic. */
function makeControllableRun() {
	const gates = [];
	const state = { started: 0, live: 0, peakLive: 0 };
	async function run() {
		state.started += 1;
		state.live += 1;
		state.peakLive = Math.max(state.peakLive, state.live);
		const gate = {};
		gate.promise = new Promise((resolve, reject) => {
			gate.resolve = resolve;
			gate.reject = reject;
		});
		gates.push(gate);
		try {
			await gate.promise;
		} finally {
			state.live -= 1;
		}
	}
	return { run, gates, state };
}

/** Let every already-queued microtask and continuation settle. */
function flush() {
	return new Promise((resolve) => setImmediate(resolve));
}

before(async () => {
	coalesceModule = await loadTypeScriptModule('src/lib/rb/coalesce.ts');
});

// ------------------------------------------------------------- the coalescer

test('an idle trigger runs immediately', async () => {
	const { run, gates, state } = makeControllableRun();
	const trigger = coalesceModule.coalesce(run);

	const pending = trigger();
	assert.equal(state.started, 1);

	gates[0].resolve();
	await pending;
	assert.equal(state.live, 0);
});

test('one frame firing both subscriptions never runs two refetches concurrently', async () => {
	const { run, gates, state } = makeControllableRun();
	const trigger = coalesceModule.coalesce(run);

	// A gap-revealing library.changed frame calls subscribeKind('tracks') AND
	// subscribeResync, both wired to the same refresh.
	const fromKind = trigger();
	const fromResync = trigger();
	assert.equal(state.started, 1, 'the second trigger joins the run in flight');

	gates[0].resolve();
	await flush();
	assert.equal(state.started, 2, 'and is answered by exactly one trailing run');
	assert.equal(state.peakLive, 1, 'never two full refetches in flight at once');

	gates[1].resolve();
	await fromKind;
	await fromResync;
	assert.equal(state.started, 2, 'and no third run');
});

test('many triggers during one run collapse into a single trailing run', async () => {
	const { run, gates, state } = makeControllableRun();
	const trigger = coalesceModule.coalesce(run);

	const first = trigger();
	trigger();
	trigger();
	trigger();
	assert.equal(state.started, 1);

	gates[0].resolve();
	await flush();
	assert.equal(state.started, 2, 'four triggers, two runs');

	gates[1].resolve();
	await first;
	assert.equal(state.started, 2);
});

test('a trigger that lands during the trailing run books one more pass', async () => {
	const { run, gates, state } = makeControllableRun();
	const trigger = coalesceModule.coalesce(run);

	trigger();
	trigger();
	gates[0].resolve();
	await flush();
	assert.equal(state.started, 2, 'the trailing run is in flight');

	// This invalidation describes a change the trailing run may already have
	// read past, so absorbing it would leave the pane stale.
	const late = trigger();
	gates[1].resolve();
	await flush();
	assert.equal(state.started, 3);

	gates[2].resolve();
	await late;
});

test('a trigger after everything settles starts a fresh run', async () => {
	const { run, gates, state } = makeControllableRun();
	const trigger = coalesceModule.coalesce(run);

	const first = trigger();
	gates[0].resolve();
	await first;

	const second = trigger();
	assert.equal(state.started, 2, 'the coalescer is idle again, not latched');
	gates[1].resolve();
	await second;
});

test('a rejected run surfaces the failure and does not wedge later triggers', async () => {
	const { run, gates, state } = makeControllableRun();
	const trigger = coalesceModule.coalesce(run);

	const first = trigger();
	const joined = trigger();
	gates[0].reject(new Error('refresh exploded'));
	await assert.rejects(first, /refresh exploded/);
	await assert.rejects(joined, /refresh exploded/, 'the joined trigger sees it too');

	const next = trigger();
	assert.equal(state.started, 2, 'a failure clears the coalescer rather than pinning it');
	gates[1].resolve();
	await next;
});

// -------------------------------------------------------- BrowserPanel wiring

test('BrowserPanel routes its bus-driven refresh through the coalescer', () => {
	const source = readFileSync(BROWSER_PANEL, 'utf8');

	assert.match(source, /import \{ coalesce \} from '\$lib\/rb\/coalesce'/);
	assert.match(
		source,
		/const _refreshLibraryRows = coalesce\(_refreshLibraryRowsOnce\)/,
		'the raw refresh must only be reachable through the coalescer'
	);
	assert.match(source, /subscribeKind\('tracks', \(\) => void _refreshLibraryRows\(\)\)/);
	assert.match(source, /subscribeResync\(\(\) => void _refreshLibraryRows\(\)\)/);
	assert.equal(
		source.split('_refreshLibraryRowsOnce').length - 1,
		2,
		'exactly two mentions: the declaration and the coalesce() call, no direct callers'
	);
});

test('the pane refresh drops a response whose playlist changed under it', () => {
	const source = readFileSync(BROWSER_PANEL, 'utf8');

	assert.match(
		source,
		/const requestedPlaylistId = p\.playlist_id;/,
		'the target is snapshotted before the fetch'
	);
	assert.match(
		source,
		/if \(p\.playlist_id !== requestedPlaylistId\) continue;/,
		'and rechecked before the rows are written'
	);
	const guard = source.indexOf('if (p.playlist_id !== requestedPlaylistId) continue;');
	const write = source.indexOf('p.rows = result.rows;');
	assert.ok(guard !== -1 && write !== -1 && guard < write, 'the guard must precede the write');
});
