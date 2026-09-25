// requirement: USBPLAY-05, USBPLAY-09
/**
 * Play from USB browse store (src/lib/rb/usb-library.svelte.ts), driven
 * against a REAL local HTTP server that serves the library route contract
 * (GET /api/v1/usb/volumes/{volume_id}/library). Nothing replaces fetch: the
 * store's own request goes over the wire and the server records it.
 *
 * Svelte runes are compile-time. The loader already stands $state in; this
 * file stands in $effect / $effect.root the same way, recording what the
 * watcher registers, because node tests cannot run Svelte effects.
 *
 * Regression lines:
 * - [if] importing the store fetches anything [then] every page load reads the
 *   stick (or logs a 503 on a machine without USB access)
 * - [if] the volume id is not sent as an encoded `vol:<uuid>` [then] the route
 *   404s for every stick
 * - [if] a second open of the same stick refetches [then] the cache is gone
 * - [if] USB_STICK_NOT_MOUNTED does not surface as "Stick removed" [then] a
 *   pulled stick shows a raw error
 * - [if] an unplug keeps the cache [then] a replugged stick serves stale rows
 * - [if] a replug does not restore the pane's rows and selection [then] the
 *   DJ must re-browse mid-set
 * - [if] a read in flight across an unplug repopulates the cache [then] a gone
 *   stick looks mounted
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const UUID_A = 'AAAAAAAA-0000-4000-8000-00000000000A';
const UUID_B = 'BBBBBBBB-0000-4000-8000-00000000000B';
const UUID_GONE = 'DDDDDDDD-0000-4000-8000-00000000000D';
const UUID_BLOCKED = 'EEEEEEEE-0000-4000-8000-00000000000E';

// ------------------------------------------------------------ fixtures

function track(uuid, pdbId) {
	return {
		id: `usb-${uuid}-${pdbId}`,
		pdb_id: pdbId,
		title: `Synthetic ${pdbId}`,
		artist: null,
		album: null,
		genre: null,
		key: '1A',
		bpm: 120,
		duration_s: 200,
		rating: 0,
		file_path: `/Contents/fixture/${pdbId}.mp3`,
		has_analysis: true,
		has_artwork: false,
		date_added: null
	};
}

function stickLibrary(uuid, name) {
	const tracks = [track(uuid, 1), track(uuid, 2), track(uuid, 3)];
	return {
		volume_id: `vol:${uuid}`,
		volume_uuid: uuid,
		name,
		mount_path: `/Volumes/${name}`,
		tracks,
		playlists: [
			{
				id: 'pl-1',
				pdb_id: 1,
				name: 'Warmup',
				parent_id: null,
				is_folder: false,
				sort_order: 1,
				track_ids: [`usb-${uuid}-3`, `usb-${uuid}-1`]
			}
		],
		history: [],
		counts: { tracks: 3, playlists: 1, playlist_entries: 2, history_playlists: 0 },
		read_ms: 4.2
	};
}

// ------------------------------------------------------------ server

/** Requests the server saw, as raw (still-encoded) URL paths. */
const hits = [];
/** uuid -> library payload served with 200; anything absent is not mounted. */
const mounted = new Map();
/** uuid -> a promise the handler awaits before answering (to hold a read). */
const holds = new Map();
/** Every hold's release, so teardown can free a hold a failed test left. */
const releases = [];

/** Hold the next reads of `uuid` until the returned release is called. */
function holdReads(uuid) {
	let release;
	holds.set(uuid, new Promise((resolve) => (release = resolve)));
	releases.push(release);
	return release;
}
let server;
let apiBase;

function sendJson(res, status, body) {
	res.writeHead(status, { 'content-type': 'application/json' });
	res.end(JSON.stringify(body));
}

async function handle(req, res) {
	hits.push(req.url);
	const match = /^\/api\/v1\/usb\/volumes\/([^/]+)\/library$/.exec(req.url);
	if (match === null) return sendJson(res, 404, { detail: { code: 'NOT_FOUND', message: req.url } });
	const volumeId = decodeURIComponent(match[1]);
	const uuid = volumeId.slice('vol:'.length);
	await holds.get(uuid);
	if (uuid === UUID_BLOCKED) {
		return sendJson(res, 503, {
			detail: { code: 'AUDIO_ACCESS_BLOCKED', message: 'removable volume access denied' }
		});
	}
	const payload = mounted.get(uuid);
	if (payload === undefined) {
		return sendJson(res, 404, {
			detail: { code: 'USB_STICK_NOT_MOUNTED', message: `no mounted stick ${uuid}`, volume_uuid: uuid }
		});
	}
	return sendJson(res, 200, payload);
}

// ------------------------------------------------------------ modules

let store;
let PaneStore;
let effects;
let hitsAtImport;

before(async () => {
	server = createServer((req, res) => {
		handle(req, res).catch((exc) => {
			res.writeHead(500);
			res.end(String(exc));
		});
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	apiBase = `http://127.0.0.1:${server.address().port}`;

	effects = [];
	globalThis.$effect = (fn) => {
		effects.push(fn);
	};
	globalThis.$effect.root = (fn) => {
		fn();
		return () => {};
	};

	store = await loadTypeScriptModule('src/lib/rb/usb-library.svelte.ts', { viteApiBase: apiBase });
	hitsAtImport = hits.length;
	({ PaneStore } = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts'));
});

after(async () => {
	delete globalThis.$effect;
	// A failed assertion can leave a held request open; close it so a red
	// run ends red instead of hanging on server.close.
	for (const release of releases) release();
	server.closeAllConnections();
	await new Promise((resolve) => server.close(resolve));
});

beforeEach(() => {
	mounted.clear();
	holds.clear();
	mounted.set(UUID_A, stickLibrary(UUID_A, 'STICK A '));
	mounted.set(UUID_B, stickLibrary(UUID_B, 'STICK B'));
});

function libraryHits(uuid) {
	return hits.filter((url) => url.includes(encodeURIComponent(`vol:${uuid}`))).length;
}

/** Let queued promise callbacks (and the server round trip) settle. */
async function settle(ms = 50) {
	await new Promise((resolve) => setTimeout(resolve, ms));
}

async function loadedPane(uuid, nodeKey, allPanes) {
	const pane = new PaneStore();
	allPanes.push(pane);
	const paneId = `usbpl:${uuid}:${nodeKey}`;
	const seq = pane.beginLoad(paneId, paneId, 'usb');
	await store.loadUsbPane(pane, seq, () => allPanes);
	return pane;
}

// ------------------------------------------------------------ tests

test('importing the store reads nothing (fetch only on a click)', () => {
	assert.equal(hitsAtImport, 0, `import-time requests: ${hits.slice(0, hitsAtImport)}`);
	assert.deepEqual(store.usbLibrary.sticks, {});
});

test('the library route gets the URL-encoded vol:<uuid> id', () => {
	assert.equal(
		store.usbLibraryUrl(UUID_A),
		`/api/v1/usb/volumes/vol%3A${UUID_A}/library`
	);
});

test('opening a stick reads it once, then serves the cache', async () => {
	const before = libraryHits(UUID_A);
	store.openUsbStick({ id: `vol:${UUID_A}` });
	assert.equal(store.usbLibrary.sticks[`vol:${UUID_A}`].status, 'loading');
	await settle();
	const view = store.usbLibrary.sticks[`vol:${UUID_A}`];
	assert.equal(view.status, 'ready');
	assert.equal(view.name, 'STICK A');
	assert.equal(view.trackCount, 3);
	assert.deepEqual(
		view.tree.map((n) => n.name),
		['All tracks', 'Warmup']
	);
	assert.ok(hits.includes(`/api/v1/usb/volumes/vol%3A${UUID_A}/library`), `hits: ${hits}`);

	store.openUsbStick({ id: `vol:${UUID_A}` });
	await Promise.all([store.ensureUsbLibrary(UUID_A), store.ensureUsbLibrary(UUID_A)]);
	assert.equal(libraryHits(UUID_A) - before, 1, 'a cached stick must not be read again');
});

test('concurrent first reads of one stick share a single request', async () => {
	const before = libraryHits(UUID_B);
	const [a, b] = await Promise.all([store.ensureUsbLibrary(UUID_B), store.ensureUsbLibrary(UUID_B)]);
	assert.equal(a, b);
	assert.equal(libraryHits(UUID_B) - before, 1);
});

test('USB_STICK_NOT_MOUNTED surfaces typed, as "Stick removed"', async () => {
	await assert.rejects(store.ensureUsbLibrary(UUID_GONE), (exc) => {
		assert.equal(exc.name, 'UsbLibraryError');
		assert.equal(exc.code, 'USB_STICK_NOT_MOUNTED');
		assert.match(exc.message, /Stick removed/);
		return true;
	});
	assert.deepEqual(store.usbLibrary.sticks[`vol:${UUID_GONE}`], {
		status: 'error',
		code: 'USB_STICK_NOT_MOUNTED',
		message: 'USB_STICK_NOT_MOUNTED: Stick removed'
	});
});

test('other route errors keep their code and the backend sentence once', async () => {
	await assert.rejects(store.ensureUsbLibrary(UUID_BLOCKED), (exc) => {
		assert.equal(exc.code, 'AUDIO_ACCESS_BLOCKED');
		assert.equal(exc.message, 'AUDIO_ACCESS_BLOCKED: removable volume access denied');
		return true;
	});
});

test('a volume without a VolumeUUID is refused before any request', () => {
	const before = hits.length;
	store.openUsbStick({ id: 'path:FIXTURE' });
	assert.equal(hits.length, before);
	assert.equal(store.usbLibrary.sticks['path:FIXTURE'].code, 'USB_VOLUME_HAS_NO_UUID');
});

test('a payload for a different stick is rejected as malformed', async () => {
	const uuid = 'CCCCCCCC-0000-4000-8000-00000000000C';
	mounted.set(uuid, stickLibrary(UUID_A, 'WRONG'));
	await assert.rejects(store.ensureUsbLibrary(uuid), (exc) => exc.code === 'USB_LIBRARY_MALFORMED');
});

test('a stick pane loads in the stick order, titled by stick, through its load token', async () => {
	const panes = [];
	const pane = await loadedPane(UUID_A, 'pl-1', panes);
	assert.equal(pane.loading, false);
	assert.equal(pane.error, null);
	assert.equal(pane.kind, 'usb');
	assert.equal(pane.title, 'STICK A / Warmup');
	assert.deepEqual(
		pane.rows.map((r) => r.stable_id),
		[`usb-${UUID_A}-3`, `usb-${UUID_A}-1`]
	);

});

test('a newer selection wins over a stick read still in flight', async () => {
	const uuid = '99999999-0000-4000-8000-000000000009';
	mounted.set(uuid, stickLibrary(uuid, 'HELD'));
	const release = holdReads(uuid);
	const pane = new PaneStore();
	const stale = pane.beginLoad(`usbpl:${uuid}:all`, 'x', 'usb');
	const loading = store.loadUsbPane(pane, stale, () => [pane]);
	await settle(20);
	// The DJ clicks a library playlist while the stick is still being read.
	pane.beginLoad('pl-9', 'Library list', 'playlist');
	release();
	await loading;
	assert.equal(pane.playlist_id, 'pl-9');
	assert.equal(pane.title, 'Library list');
	assert.deepEqual(pane.rows, []);
	assert.equal(pane.loading, true, 'the newer load still owns the pane');
});

test('two sticks keep separate panes and rows', async () => {
	const panes = [];
	const a = await loadedPane(UUID_A, 'all', panes);
	const b = await loadedPane(UUID_B, 'all', panes);
	assert.ok(a.rows.every((r) => r.stable_id.startsWith(`usb-${UUID_A}-`)));
	assert.ok(b.rows.every((r) => r.stable_id.startsWith(`usb-${UUID_B}-`)));
	assert.equal(b.title, 'STICK B / All tracks');
});

test('unplug grays the pane in place; replug restores rows and selection', async () => {
	const panes = [];
	const pane = await loadedPane(UUID_A, 'pl-1', panes);
	const other = await loadedPane(UUID_B, 'all', panes);
	pane.select(`usb-${UUID_A}-1`, false);
	const selected = { id: pane.selected_id, order: pane.selected_order };
	assert.equal(selected.id, `usb-${UUID_A}-1`);

	// Both sticks present first, so the tracker's view matches the loads.
	store.applyUsbVolumePresence([
		{ id: `vol:${UUID_A}`, present: true },
		{ id: `vol:${UUID_B}`, present: true }
	]);

	// Stick A is pulled.
	mounted.delete(UUID_A);
	const changed = store.applyUsbVolumePresence([{ id: `vol:${UUID_B}`, present: true }]);
	assert.deepEqual(changed, [UUID_A]);
	assert.equal(pane.title, 'STICK A / Warmup (stick removed)');
	assert.deepEqual(
		pane.rows.map((r) => [r.stable_id, r.file_exists, r.file_availability]),
		[
			[`usb-${UUID_A}-3`, false, 'awaiting_volume'],
			[`usb-${UUID_A}-1`, false, 'awaiting_volume']
		]
	);
	assert.deepEqual({ id: pane.selected_id, order: pane.selected_order }, selected);
	assert.equal(store.usbLibrary.sticks[`vol:${UUID_A}`], undefined, 'the cache view must be dropped');
	// The other stick is untouched.
	assert.ok(other.rows.every((r) => r.file_exists === true));

	// A background refresh while removed keeps the grayed pane, no raw error.
	await store.refreshUsbPane(pane);
	assert.equal(pane.title, 'STICK A / Warmup (stick removed)');
	assert.ok(pane.rows.every((r) => r.file_exists === false));

	// The same stick returns: its pane re-reads and comes back as it was.
	mounted.set(UUID_A, stickLibrary(UUID_A, 'STICK A '));
	const readsBefore = libraryHits(UUID_A);
	const back = store.applyUsbVolumePresence([
		{ id: `vol:${UUID_A}`, present: true },
		{ id: `vol:${UUID_B}`, present: true }
	]);
	assert.deepEqual(back, [UUID_A]);
	await settle();
	assert.equal(libraryHits(UUID_A) - readsBefore, 1, 'a replug must re-read the stick');
	assert.equal(pane.title, 'STICK A / Warmup');
	assert.deepEqual(
		pane.rows.map((r) => [r.stable_id, r.file_exists, r.file_availability]),
		[
			[`usb-${UUID_A}-3`, true, 'present'],
			[`usb-${UUID_A}-1`, true, 'present']
		]
	);
	assert.deepEqual({ id: pane.selected_id, order: pane.selected_order }, selected);
	assert.equal(store.usbLibrary.sticks[`vol:${UUID_A}`].status, 'ready');
});

test('a first load of a removed stick fails typed instead of inventing rows', async () => {
	const pane = new PaneStore();
	const seq = pane.beginLoad(`usbpl:${UUID_GONE}:all`, 'x', 'usb');
	await assert.rejects(
		store.loadUsbPane(pane, seq, () => [pane]),
		(exc) => exc.code === 'USB_STICK_NOT_MOUNTED' && /Stick removed/.test(exc.message)
	);
});

test('a read in flight across an unplug does not repopulate the cache', async () => {
	const uuid = 'FFFFFFFF-0000-4000-8000-00000000000F';
	mounted.set(uuid, stickLibrary(uuid, 'SLOW'));
	const release = holdReads(uuid);
	store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: true }]);
	const read = store.ensureUsbLibrary(uuid);
	await settle(20);
	store.applyUsbVolumePresence([]); // unplugged while the server holds the read
	release();
	await assert.rejects(read, (exc) => exc.code === 'USB_STICK_NOT_MOUNTED');
	assert.equal(store.usbLibrary.sticks[`vol:${uuid}`], undefined);
	// The next open reads again instead of serving what the dead read fetched.
	const before = libraryHits(uuid);
	await store.ensureUsbLibrary(uuid);
	assert.equal(libraryHits(uuid) - before, 1);
});

test('a replug seen by the tree before the watcher still restores the pane', async () => {
	// The stick row remounts its tree (ensureUsbLibrary) as soon as the stick
	// is listed again, which can run BEFORE the volume watcher's next pass.
	// That read must not hide the replug from the watcher.
	const uuid = '77777777-0000-4000-8000-000000000007';
	mounted.set(uuid, stickLibrary(uuid, 'ORDER'));
	const panes = [];
	store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: true }]);
	const pane = await loadedPane(uuid, 'pl-1', panes);
	store.applyUsbVolumePresence([]);
	assert.ok(pane.rows.every((r) => r.file_exists === false));

	await store.ensureUsbLibrary(uuid); // the tree remount wins the race
	const changed = store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: true }]);
	assert.deepEqual(changed, [uuid]);
	await settle();
	assert.ok(pane.rows.every((r) => r.file_exists === true), 'the pane must come back');
	assert.equal(pane.title, 'ORDER / Warmup');
});

test('a dead read settling late does not orphan the live one', async () => {
	const uuid = '88888888-0000-4000-8000-000000000008';
	mounted.set(uuid, stickLibrary(uuid, 'LATE'));
	const releaseFirst = holdReads(uuid);
	store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: true }]);
	const first = store.ensureUsbLibrary(uuid).catch((exc) => exc);
	await settle(20);
	store.applyUsbVolumePresence([]); // unplug: the first read is now dead
	store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: true }]); // replug
	const releaseSecond = holdReads(uuid);
	const second = store.ensureUsbLibrary(uuid);
	await settle(20);
	releaseFirst();
	assert.equal((await first).code, 'USB_STICK_NOT_MOUNTED');
	// The live read is still shared: no third request.
	const before = libraryHits(uuid);
	const third = store.ensureUsbLibrary(uuid);
	assert.equal(third, second, 'a caller after the dead read settles must share the live read');
	releaseSecond();
	await Promise.all([second, third]);
	assert.equal(libraryHits(uuid) - before, 0);
});

test('the presence watcher is armed once, and it feeds the volume list in', async () => {
	// Every open and pane load above called watchUsbVolumes; one effect only.
	assert.equal(effects.length, 1, `watcher effects registered: ${effects.length}`);
	await store.ensureUsbLibrary(UUID_B);
	store.applyUsbVolumePresence([{ id: `vol:${UUID_B}`, present: true }]);
	assert.equal(store.usbLibrary.sticks[`vol:${UUID_B}`].status, 'ready');
	// Under node the tracker has no stored volumes, so its list is empty:
	// running the registered effect must read that list and drop stick B.
	effects[0]();
	assert.equal(store.usbLibrary.sticks[`vol:${UUID_B}`], undefined);
});
