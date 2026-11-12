// LIBTREE-01/02: the tree order writer against the PRODUCTION route.
//
// Codex P1 on #6186 (discussion_r4264403126): these tests used to run against
// a hand-written Node handler. They now spawn fixtures/playlist_tree_order_server.py,
// a real uvicorn server on the production create_app, so every PUT goes through
// the real putPlaylistTreeOrder, real fetch, and FastAPI put_playlist_tree_order
// with its real validation, overlay file, event publication and 422 mapping.
// Failures are real refusals (an order naming a playlist that does not exist),
// and every "stored" claim is read back from the server.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { fixtureServerLaunch } from '../../scripts/fixture-server.mjs';
import { resolveUv } from './fixtures/resolve-uv.mjs';
import { stopFixtureServer } from './fixtures/stop-fixture-server.mjs';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const SERVER_SCRIPT = fileURLToPath(
	new URL('./fixtures/playlist_tree_order_server.py', import.meta.url)
);

let m;
let writeApi;
let serverProcess;
let apiBase;

before(async () => {
	const launch = fixtureServerLaunch({ repoRoot: REPOSITORY_ROOT, pythonTarget: SERVER_SCRIPT });
	serverProcess = spawn(launch.command, launch.args, {
		cwd: launch.cwd,
		env: launch.env,
		stdio: ['ignore', 'pipe', 'inherit']
	});
	const port = await new Promise((resolve, reject) => {
		serverProcess.once('exit', (code) =>
			reject(new Error(`fixture server exited early (${code})`))
		);
		createInterface({ input: serverProcess.stdout }).on('line', (line) => {
			const ready = /^READY (\d+)$/.exec(line);
			if (ready) resolve(Number(ready[1]));
		});
	});
	apiBase = `http://127.0.0.1:${port}`;
	m = await loadTypeScriptModule('src/lib/components/rb/browser/playlist-tree-order.ts');
	writeApi = await loadTypeScriptModule('src/lib/rb/playlist-write.ts', { viteApiBase: apiBase });
});

after(async () => {
	if (serverProcess) await stopFixtureServer(serverProcess, apiBase);
});

async function call(method, path, body) {
	const res = await fetch(`${apiBase}${path}`, {
		method,
		headers: body === undefined ? {} : { 'content-type': 'application/json' },
		body: body === undefined ? undefined : JSON.stringify(body)
	});
	assert.ok(res.ok, `${method} ${path} answered ${res.status}: ${await res.clone().text()}`);
	return res.json();
}

async function serverRows() {
	const rows = await call('GET', '/api/v1/playlists?availability=skip');
	return rows.map(({ playlist_id, name, seq }) => ({ playlist_id, name, seq }));
}

const storedOrder = async () => (await call('GET', '/api/v1/playlist-tree/order')).order;
const putsSeen = async () => (await call('GET', '/test/puts')).seen;

let made = 0;
/** Fresh real playlists for one test, laid out in the given order on the server. */
async function seed(names) {
	const ids = [];
	for (const name of names) {
		made += 1;
		ids.push((await call('POST', '/api/v1/playlists', { name: `${name}-${made}` })).playlist_id);
	}
	await call('PUT', '/api/v1/playlist-tree/order', { order: ids });
	return ids;
}

// Wired exactly as BrowserPanel wires it: put is the real putPlaylistTreeOrder,
// refresh re-reads the real GET /playlists. Only the Svelte rows and the toast
// sink are local, because node tests do not render.
async function harness({ refreshDown = false } = {}) {
	const state = { rows: await serverRows(), toasts: [], refreshes: 0 };
	const writer = m.createTreeOrderWriter({
		read: () => state.rows,
		write: (next) => {
			state.rows = next;
		},
		put: writeApi.putPlaylistTreeOrder,
		refresh: async () => {
			state.refreshes += 1;
			// refreshDown: the follow-up GET cannot reach the server, so the rows
			// shown are only what the writer itself put back.
			if (refreshDown) throw new Error('refresh unreachable');
			state.rows = await serverRows();
		},
		notify: (message, kind) => state.toasts.push([kind, message])
	});
	return { state, writer };
}

async function until(check, what) {
	for (let i = 0; i < 500; i += 1) {
		if (await check()) return;
		await new Promise((r) => setTimeout(r, 10));
	}
	assert.fail(`timed out waiting for ${what}`);
}

const shown = (state, ids) => m.treeOrderIds(state.rows).filter((id) => ids.includes(id));
const GHOST = 'no-such-playlist';

test('a successful write is stored by the real route and repainted from it', async () => {
	const [a, b] = await seed(['A', 'B']);
	const { state, writer } = await harness();
	await writer([b, a], 'Playlists sorted A-Z');
	assert.deepEqual((await storedOrder()).slice(0, 2), [b, a], 'the overlay holds the new order');
	assert.deepEqual(shown(state, [a, b]), [b, a]);
	assert.deepEqual(state.toasts, [['info', 'Playlists sorted A-Z']]);
	assert.equal(state.refreshes, 1);
});

test('a second drag waits for the first write, so the newest order is stored last', async () => {
	const [a, b, c] = await seed(['A', 'B', 'C']);
	const { state, writer } = await harness();
	const seenBefore = await putsSeen();
	await call('POST', '/test/hold-next-put');
	const first = writer([b, a, c]);
	const second = writer([c, b, a]);
	const third = writer([c, a, b]);
	assert.deepEqual(shown(state, [a, b, c]), [c, a, b], 'the newest drag repaints at once');
	await until(
		async () => (await putsSeen()) === seenBefore + 1,
		'the first PUT to reach the route'
	);
	// Only one PUT is ever in flight: the queued ones have not reached the route.
	await new Promise((r) => setTimeout(r, 100));
	assert.equal(await putsSeen(), seenBefore + 1);
	assert.equal((await call('POST', '/test/release-put')).released, true);
	await Promise.all([first, second, third]);
	// The middle drag was superseded before it started: two PUTs in all.
	assert.equal(await putsSeen(), seenBefore + 2);
	assert.deepEqual(await storedOrder(), [c, a, b], 'the newest order is what the route stored');
	assert.deepEqual(shown(state, [a, b, c]), [c, a, b]);
	assert.equal(state.refreshes, 1, 'only the newest write repaints from the server');
});

test('a write the real route refuses restores the pre-write rows and keeps the stored order', async () => {
	const [a, b] = await seed(['A', 'B']);
	const { state, writer } = await harness({ refreshDown: true });
	const before = state.rows;
	await writer([b, a, GHOST], 'Playlists sorted A-Z');
	assert.deepEqual(state.rows, before, 'the snapshot is back, not the optimistic order');
	assert.equal(state.toasts.length, 1);
	assert.equal(state.toasts[0][0], 'error');
	assert.match(state.toasts[0][1], /^reorder playlists failed \(422\): .*no-such-playlist/);
	assert.deepEqual(await storedOrder(), [a, b], 'the route wrote nothing');
});

test('a refused burst restores the rows from before the first drag', async () => {
	const [a, b, c] = await seed(['A', 'B', 'C']);
	const { state, writer } = await harness({ refreshDown: true });
	const before = state.rows;
	const seenBefore = await putsSeen();
	await call('POST', '/test/hold-next-put');
	const first = writer([b, a, GHOST]);
	const second = writer([c, b, a, GHOST]);
	await until(async () => (await putsSeen()) === seenBefore + 1, 'the first PUT');
	await call('POST', '/test/release-put');
	await Promise.all([first, second]);
	assert.equal(await putsSeen(), seenBefore + 2, 'the queued drag still reached the route');
	assert.deepEqual(state.rows, before);
	assert.equal(state.toasts.length, 1, 'one error for the burst, not one per drag');
	assert.deepEqual(await storedOrder(), [a, b, c]);
});

test('a refused write after a stored one restores the stored order, not the pre-burst rows', async () => {
	const [a, b, c] = await seed(['A', 'B', 'C']);
	const { state, writer } = await harness({ refreshDown: true });
	const seenBefore = await putsSeen();
	await call('POST', '/test/hold-next-put');
	const first = writer([b, a, c]);
	const second = writer([c, b, a, GHOST]);
	await until(async () => (await putsSeen()) === seenBefore + 1, 'the first PUT');
	await call('POST', '/test/release-put');
	await Promise.all([first, second]);
	assert.deepEqual(await storedOrder(), [b, a, c], 'the route stored the first order only');
	assert.deepEqual(shown(state, [a, b, c]), [b, a, c], 'the UI shows what the server holds');
	assert.equal(state.toasts.length, 1);
	assert.equal(state.toasts[0][0], 'error');
});

test('a playlists event read from an earlier PUT cannot revert a queued newer order', async () => {
	// Codex P2 on #6186 (r4265141178). Wired as BrowserPanel wires it: every
	// playlist re-read goes through one createLatestRead, and its apply drops
	// rows with writer.overlay.
	const [a, b, c] = await seed(['A', 'B', 'C']);
	const { state, writer } = await harness();
	const readPlaylists = m.createLatestRead(serverRows);
	const eventRefresh = () =>
		readPlaylists((rows) => {
			state.rows = writer.overlay(rows);
		});
	const seenBefore = await putsSeen();
	assert.equal(writer.busy(), false);
	await call('POST', '/test/hold-next-put');
	const first = writer([b, a, c]);
	const second = writer([c, b, a]);
	assert.equal(writer.busy(), true);
	await until(async () => (await putsSeen()) === seenBefore + 1, 'the first PUT');
	// Park the queued PUT too, then let the first one store.
	await call('POST', '/test/hold-next-put');
	await call('POST', '/test/release-put');
	await until(async () => (await putsSeen()) === seenBefore + 2, 'the queued PUT');
	assert.deepEqual(await storedOrder(), [b, a, c], 'only the first order is stored so far');
	// The first PUT's playlists event: its GET finishes now, before the second store.
	await eventRefresh();
	assert.deepEqual(shown(state, [a, b, c]), [c, b, a], 'the newer optimistic order survives');
	assert.equal(writer.busy(), true);
	await call('POST', '/test/release-put');
	await Promise.all([first, second]);
	assert.equal(writer.busy(), false);
	assert.deepEqual(await storedOrder(), [c, b, a]);
	assert.deepEqual(
		shown(state, [a, b, c]),
		[c, b, a],
		'the idle refresh repaints the stored order'
	);
	// Once idle, event reads apply again.
	await eventRefresh();
	assert.deepEqual(shown(state, [a, b, c]), [c, b, a]);
});

test('a playlist created during a write shows at once and keeps the optimistic order', async () => {
	// Codex P2 on #6186 (r4266059809): the create read applies through overlay.
	const [a, b] = await seed(['A', 'B']);
	const { state, writer } = await harness();
	const readPlaylists = m.createLatestRead(serverRows);
	const seenBefore = await putsSeen();
	await call('POST', '/test/hold-next-put');
	const pending = writer([b, a]);
	await until(async () => (await putsSeen()) === seenBefore + 1, 'the PUT');
	const made = (await call('POST', '/api/v1/playlists', { name: 'Made-mid-write' })).playlist_id;
	// The create's GET sees the old stored order [a, b] plus the new row.
	await readPlaylists((rows) => {
		state.rows = writer.overlay(rows);
	});
	assert.ok(
		state.rows.some((p) => p.playlist_id === made),
		'the new playlist shows'
	);
	assert.deepEqual(shown(state, [a, b]), [b, a], 'the optimistic order survives the create read');
	await call('POST', '/test/release-put');
	await pending;
	assert.equal(writer.busy(), false);
	assert.deepEqual(shown(state, [a, b]), [b, a]);
	assert.ok(state.rows.some((p) => p.playlist_id === made));
	// Idle: overlay hands the rows back untouched.
	const rows = await serverRows();
	assert.equal(writer.overlay(rows), rows);
});

test('a refused write rolls back only the order, keeping a create and rename made meanwhile', async () => {
	// Codex P2 on #6186 (r4266059812). refreshDown: the rows shown after the
	// failure are only what the writer itself put back.
	const [a, b] = await seed(['A', 'B']);
	const { state, writer } = await harness({ refreshDown: true });
	const readPlaylists = m.createLatestRead(serverRows);
	const seenBefore = await putsSeen();
	await call('POST', '/test/hold-next-put');
	const pending = writer([b, a, GHOST]);
	await until(async () => (await putsSeen()) === seenBefore + 1, 'the PUT');
	const made = (await call('POST', '/api/v1/playlists', { name: 'Made-mid-burst' })).playlist_id;
	const head = await fetch(`${apiBase}/api/v1/playlists/${a}/tracks`);
	const etag = head.headers.get('etag');
	await head.arrayBuffer();
	assert.ok(etag, 'the playlist read carries an etag');
	const renamed = await fetch(`${apiBase}/api/v1/playlists/${a}`, {
		method: 'PATCH',
		headers: { 'content-type': 'application/json', 'if-match': etag },
		body: JSON.stringify({ name: 'Renamed-mid-burst' })
	});
	assert.ok(renamed.ok, `rename answered ${renamed.status}: ${await renamed.text()}`);
	await readPlaylists((rows) => {
		state.rows = writer.overlay(rows);
	});
	await call('POST', '/test/release-put');
	await pending;
	assert.equal(state.toasts.length, 1);
	assert.equal(state.toasts[0][0], 'error');
	assert.deepEqual(shown(state, [a, b]), [a, b], 'the stored order is back');
	assert.ok(
		state.rows.some((p) => p.playlist_id === made),
		'the create made during the burst stays'
	);
	assert.equal(
		state.rows.find((p) => p.playlist_id === a).name,
		'Renamed-mid-burst',
		'the rename stays'
	);
	assert.deepEqual((await storedOrder()).slice(0, 2), [a, b]);
});

test('resolveUv returns an absolute executable and honours MDT_UV_BIN', async () => {
	const { isAbsolute } = await import('node:path');
	const found = resolveUv();
	assert.ok(isAbsolute(found), found);
	assert.equal(resolveUv({ ...process.env, MDT_UV_BIN: found }), found);
	assert.throws(
		() => resolveUv({ MDT_UV_BIN: `${found}.missing` }),
		/MDT_UV_BIN=.* is not an executable file/
	);
});

test('resolveUv makes a relative PATH entry absolute before checking it', async () => {
	const { dirname, relative, isAbsolute, basename } = await import('node:path');
	const found = resolveUv();
	const rel = relative(process.cwd(), dirname(found)) || '.';
	assert.ok(!isAbsolute(rel), rel);
	const got = resolveUv({ PATH: rel, PATHEXT: process.env.PATHEXT });
	assert.ok(isAbsolute(got), got);
	assert.equal(basename(got).toLowerCase(), basename(found).toLowerCase());
	assert.equal(dirname(got), dirname(found));
});
