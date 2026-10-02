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
 * - if BrowserPanel stops routing its bus subscriptions through the gate that
 *   owns the coalescer then C5 is back
 * - if _refreshLibraryRowsOnce stops rechecking playlist_id after its await
 *   then a playlist switch mid-fetch paints pane B with pane A's rows
 * - if a bus subscription calls _refreshLibraryRowsOnce directly again then the
 *   PERFMODE-04 playing gate is bypassed and mid-set refetches are back
 *
 * The gate's own behavior (defer, drain, never drop, one ring row, and that its
 * coalescer still holds under the gate) lives in playing-gate.test.mjs.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const BROWSER_PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);
const PLAYING_GATE = fileURLToPath(new URL('../../src/lib/rb/playing-gate.ts', import.meta.url));

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

test('the gate that BrowserPanel uses owns the coalescer', () => {
	const gate = readFileSync(PLAYING_GATE, 'utf8');

	assert.match(gate, /import \{ coalesce \} from '\$lib\/rb\/coalesce'/);
	assert.match(
		gate,
		/const trigger = coalesce\(options\.run\)/,
		'the raw refresh must only be reachable through the coalescer'
	);
	assert.doesNotMatch(
		gate,
		/options\.run\(\)/,
		'no path may call the wrapped work directly and skip the coalescer'
	);
});

test('BrowserPanel routes its bus-driven refresh through that gate', () => {
	const source = readFileSync(BROWSER_PANEL, 'utf8');

	assert.match(
		source,
		/anyDeckPlaying,\s*createPlayingGate,[\s\S]*?from '\.\/browser\/browser-panel-support'/,
		'the playing gate is imported through browser-panel-support so audio-engine.svelte.ts stays under the file-size cap'
	);
	assert.match(source, /run: _refreshLibraryRowsOnce/);
	assert.match(source, /isPlaying: anyDeckPlaying/);
	assert.match(source, /kind: 'library-refresh-deferred'/);
	assert.equal(
		source.split('_refreshLibraryRowsOnce(').length - 1,
		1,
		'the declaration is the only call-shaped occurrence: nothing invokes it directly'
	);

	// Every background trigger goes through request(), never straight to the
	// refresh: a direct call is the mid-set stall this gate exists to stop.
	assert.match(source, /subscribeKind\('tracks', \(\) => _libraryRefreshGate\.request\(\)\)/);
	assert.match(
		source,
		/subscribeKind\('playlists', \(\) => \{\s*(?:invalidateAllPlaylistFirstPages\(\);\s*)?void _refreshPlaylists\(\);\s*_libraryRefreshGate\.request\(\);\s*\}\)/,
		'a playlist rename from the write API or undo stack must update tree names even if the full library refetch is in flight, deferred, or throws'
	);
	assert.match(
		source,
		/subscribeResync\(\(\) => \{\s*(?:invalidateAllPlaylistFirstPages\(\);\s*)?void _refreshPlaylists\(\);\s*_libraryRefreshGate\.request\(\);\s*\}\)/,
		'a missed playlist invalidation on reconnect must refresh tree names, not only pane rows'
	);
	assert.match(source, /subscribeKind\('smartlists', \(\) => _libraryRefreshGate\.request\(\)\)/);
	assert.match(source, /onselectsmartlist=\{selectSmartlist\}/);
	assert.match(source, /async function _fetchSmartlistRows/);
	assert.match(source, /p\.kind === 'smartlist'/);
	const requestCallSites = source
		.split('\n')
		.filter((line) => line.includes('_libraryRefreshGate.request()'))
		.filter((line) => !/^\s*(\/\/|\*)/.test(line));
	assert.equal(
		requestCallSites.length,
		5,
		'the four bus subscriptions (tracks, playlists, smartlists, resync) plus the 60s degraded-path poll, and nothing else'
	);
});

test('the deferred refresh drains on a stop and on a library interaction', () => {
	const source = readFileSync(BROWSER_PANEL, 'utf8');

	// The reactive read inside the effect is what subscribes it to every deck's
	// transport; without it the gate would need a poll of its own.
	assert.match(
		source,
		/\$effect\(\(\) => \{\s*if \(!anyDeckPlaying\(\)\) untrack\(\(\) => _libraryRefreshGate\.drain\(\)\);\s*\}\);/,
		'a stop must drain the owed refresh, or the pane is stale for the session; ' +
			'untracked, or the refresh it starts becomes a dependency of the effect'
	);
	assert.match(source, /_libraryRefreshGate\.flushOnInteraction\(\);/);
	assert.equal(
		source.split('_noteLibraryInteraction();').length - 1,
		3,
		'scroll, row select and search all count as asking for fresh rows'
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
