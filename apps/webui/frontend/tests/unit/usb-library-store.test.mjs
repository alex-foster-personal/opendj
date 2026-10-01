// requirement: USBPLAY-05, USBPLAY-09
/**
 * Play from USB browse store (src/lib/rb/usb-library.svelte.ts), driven
 * against a REAL local HTTP server that serves the library route contract
 * (GET /api/v1/usb/volumes/{volume_id}/library) AND the volume list the
 * tracker polls (GET /api/v1/usb/volumes). Nothing replaces fetch: the
 * store's and the tracker's requests go over the wire.
 *
 * Unplug and replug go through the tracker's real poll, the way the app sees
 * them: the daemon stops listing a stick, and the tracker keeps its row with
 * present:false. Svelte runes are compile-time. The loader already stands
 * $state in; this file stands in $effect / $effect.root the same way,
 * recording what the watcher registers, and runs that effect after each poll
 * (or at any other moment a test chooses), because node tests cannot run
 * Svelte effects.
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
 * - [if] the watcher reads a stick the tracker has not listed yet as unplugged
 *   [then] a pane an agent opens before the first poll fails "Stick removed"
 *   although the stick is mounted
 * - [if] a refresh that outlived a pane switch still publishes [then] stick
 *   rows paint over the list the DJ switched to
 * - [if] an unplug touches a pane that is still loading [then] the pane's
 *   in-flight load is second-guessed mid-flight
 * - [if] a failed stick load toasts the raw error class [then] the DJ reads
 *   "UsbLibraryError: USB_STICK_NOT_MOUNTED" instead of "Stick removed"
 * - [if] a pane load or refresh is served from the frontend cache without
 *   asking the backend [then] an export rewritten while the stick stays
 *   mounted (or swapped between two presence polls) keeps its stale rows, and
 *   a reassigned track id shows one track while the deck loads another
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
/** What GET /api/v1/usb/volumes lists: the daemon lists present mounts only. */
let daemonVolumes = [];
let scans = 0;
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
	if (req.url === '/api/v1/usb/volumes') {
		scans += 1;
		return sendJson(res, 200, { volumes: daemonVolumes, scanned_at: scans, watching: false });
	}
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
let canMutatePlaylist;
let effects;
let hitsAtImport;
let effectsAtImport;

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

	store = await loadTypeScriptModule('tests/unit/fixtures/usb-library-store-entry.ts', {
		viteApiBase: apiBase
	});
	hitsAtImport = hits.length;
	effectsAtImport = effects.length;
	({ PaneStore, canMutatePlaylist } = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pane-contract.svelte.ts'
	));
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

/** Poll real state until `condition` holds, failing with `what` after
 * `timeoutMs`. A fixed settle() is a guess at how long the server round trip
 * takes, and a loaded CI runner outlasts the guess; this waits for the state
 * itself and still fails loudly when it never arrives. */
async function waitFor(condition, what, timeoutMs = 5000) {
	const deadline = Date.now() + timeoutMs;
	while (!condition()) {
		if (Date.now() > deadline) throw new Error(`timed out after ${timeoutMs} ms waiting for ${what}`);
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
}

async function loadedPane(uuid, nodeKey, allPanes) {
	const pane = new PaneStore();
	allPanes.push(pane);
	const paneId = `usbpl:${uuid}:${nodeKey}`;
	const seq = pane.beginLoad(paneId, paneId, 'usb');
	assert.equal(await store.loadUsbPane(pane, seq, () => allPanes), null, 'the load failed');
	return pane;
}

/** The presence watcher, run as Svelte would after the tracker reassigns
 * its list. Armed by the first stick open. */
function runWatcher() {
	assert.equal(effects.length, 1, `watcher effects registered: ${effects.length}`);
	effects[0]();
}

function daemonStick(uuid) {
	return {
		id: `vol:${uuid}`,
		name: `FIXTURE ${uuid.slice(0, 4)}`,
		mount_path: `/Volumes/FIXTURE ${uuid.slice(0, 4)}`,
		kind: 'rekordbox',
		present: true,
		simulated: false,
		is_music: true,
		role: 'usb_stick',
		access: 'ok'
	};
}

/** The daemon now lists exactly these sticks: the real tracker polls it
 * (keeping every other stick it has seen as present:false), then the
 * watcher runs. */
async function daemonLists(...uuids) {
	daemonVolumes = uuids.map(daemonStick);
	await store.refreshUsbVolumes();
	assert.equal(store.usbTracker.lastError, null, `poll failed: ${store.usbTracker.lastError}`);
	runWatcher();
}

function trackerRow(uuid) {
	return store.usbTracker.volumes.find((v) => v.id === `vol:${uuid}`);
}

const LIBRARY_ROW = { stable_id: 'a'.repeat(40), title: 'Library track', file_exists: true };

// ------------------------------------------------------------ tests

test('importing the store reads nothing and arms nothing (fetch only on a click)', () => {
	assert.equal(hitsAtImport, 0, `import-time requests: ${hits.slice(0, hitsAtImport)}`);
	assert.equal(scans, 0, 'importing must not poll the volume list');
	assert.equal(effectsAtImport, 0, 'the presence watcher arms on the first open, not at import');
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
	// Join the read openUsbStick started: an in-flight read is shared, so this
	// waits for that exact request (the hit count below proves it) and never a
	// guessed interval.
	await store.ensureUsbLibrary(UUID_A);
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

test('a pane load or refresh asks the backend again, so a rewritten export replaces stale rows', async () => {
	const allPanes = [];
	const pane = await loadedPane(UUID_A, 'all', allPanes);
	assert.deepEqual(
		pane.rows.map((row) => row.stable_id),
		[1, 2, 3].map((n) => `usb-${UUID_A}-${n}`)
	);
	// export.pdb is rewritten while the stick stays mounted: the presence
	// watcher sees no change, and the backend (which stats export.pdb on every
	// request) now serves the new library.
	const rewritten = stickLibrary(UUID_A, 'STICK A ');
	rewritten.tracks = [1, 3, 4, 5].map((n) => track(UUID_A, n));
	rewritten.counts = { ...rewritten.counts, tracks: 4 };
	mounted.set(UUID_A, rewritten);

	let before = libraryHits(UUID_A);
	await store.refreshUsbPane(pane);
	assert.equal(libraryHits(UUID_A) - before, 1, 'a refresh must ask the backend, not the frontend cache');
	assert.deepEqual(
		pane.rows.map((row) => row.stable_id),
		[1, 3, 4, 5].map((n) => `usb-${UUID_A}-${n}`),
		'the refreshed pane shows the rewritten export'
	);
	assert.equal(store.usbLibrary.sticks[`vol:${UUID_A}`].trackCount, 4, 'the stick tree follows');

	before = libraryHits(UUID_A);
	const second = await loadedPane(UUID_A, 'pl-1', allPanes);
	assert.equal(libraryHits(UUID_A) - before, 1, 'opening a pane must ask the backend too');
	assert.deepEqual(
		second.rows.map((row) => row.stable_id),
		[`usb-${UUID_A}-3`, `usb-${UUID_A}-1`]
	);

	// The opposite direction: re-opening the stick in the tree is still the
	// cache, and two refreshes at once still share one request.
	before = libraryHits(UUID_A);
	store.openUsbStick({ id: `vol:${UUID_A}` });
	await store.ensureUsbLibrary(UUID_A);
	assert.equal(libraryHits(UUID_A) - before, 0, 'a tree open of a cached stick must not read again');
	assert.equal(store.usbLibrary.sticks[`vol:${UUID_A}`].status, 'ready', 'no loading flash on a tree re-open');
	await Promise.all([store.refreshUsbPane(pane), store.refreshUsbPane(second)]);
	assert.equal(libraryHits(UUID_A) - before, 1, 'concurrent revalidations share one request');
});

test('a revalidation that fails drops the stale library instead of serving it', async () => {
	const allPanes = [];
	const pane = await loadedPane(UUID_A, 'all', allPanes);
	assert.equal(pane.rows.length, 3);
	// The stick is gone but the presence poll has not seen it yet.
	mounted.delete(UUID_A);
	await store.refreshUsbPane(pane);
	assert.ok(pane.title.endsWith('(stick removed)'), pane.title);
	assert.ok(pane.rows.every((row) => row.file_exists === false), 'rows gray in place');
	assert.equal(store.usbLibrary.sticks[`vol:${UUID_A}`].status, 'error');
	await assert.rejects(store.ensureUsbLibrary(UUID_A), /USB_STICK_NOT_MOUNTED/, 'no stale cache is served');
	// Back on the next read.
	mounted.set(UUID_A, stickLibrary(UUID_A, 'STICK A '));
	await store.refreshUsbPane(pane);
	assert.ok(pane.rows.every((row) => row.file_exists === true));
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

test('a stick pane loads in the stick order, titled by stick, read only', async () => {
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
	// Read only: no etag, so BrowserPanel offers no remove or reorder on it.
	assert.equal(pane.etag, '');
	assert.equal(canMutatePlaylist(pane), false);
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
	assert.equal(await loading, null, 'a superseded load has nothing to toast');
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

	// Both sticks listed first, so the tracker's view matches the loads.
	await daemonLists(UUID_A, UUID_B);
	assert.equal(pane.title, 'STICK A / Warmup');

	// Stick A is pulled: the daemon stops listing it, and the tracker keeps
	// its row as present:false (it never drops a stick it has seen).
	mounted.delete(UUID_A);
	await daemonLists(UUID_B);
	assert.equal(trackerRow(UUID_A).present, false, 'the tracker shape this test relies on');
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
	await daemonLists(UUID_A, UUID_B);
	await waitFor(() => pane.rows.some((r) => r.file_exists === true), 'the replugged pane to refresh');
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

test('presence: a stick the list names as not present is unplugged; one it does not name is unknown', async () => {
	const uuid = '66666666-0000-4000-8000-000000000006';
	mounted.set(uuid, stickLibrary(uuid, 'KNOWN'));
	await store.ensureUsbLibrary(uuid);
	// Not named at all: nothing is known about it, so nothing changes.
	assert.deepEqual(store.applyUsbVolumePresence([]), []);
	assert.equal(store.usbLibrary.sticks[`vol:${uuid}`].status, 'ready');
	// Named, present: still no change.
	assert.deepEqual(store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: true }]), []);
	// Named, not present: that is an unplug.
	assert.deepEqual(store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: false }]), [uuid]);
	assert.equal(store.usbLibrary.sticks[`vol:${uuid}`], undefined);
	// Named present again: a replug.
	assert.deepEqual(store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: true }]), [uuid]);
});

test('a first load of a removed stick fails in the DJ\'s words instead of inventing rows', async () => {
	const pane = new PaneStore();
	const seq = pane.beginLoad(`usbpl:${UUID_GONE}:all`, 'x', 'usb');
	// The words BrowserPanel toasts: no error class, no code (spec 4b).
	assert.equal(await store.loadUsbPane(pane, seq, () => [pane]), 'Stick removed');
	assert.equal(pane.error, 'Stick removed');
	assert.equal(pane.loading, false);
	assert.deepEqual(pane.rows, []);
});

test('an unknown stick failure keeps its typed cause in the toast', async () => {
	const pane = new PaneStore();
	const seq = pane.beginLoad(`usbpl:${UUID_BLOCKED}:all`, 'x', 'usb');
	assert.equal(
		await store.loadUsbPane(pane, seq, () => [pane]),
		'could not read this stick (AUDIO_ACCESS_BLOCKED: removable volume access denied)'
	);
});

test('a read in flight across an unplug does not repopulate the cache', async () => {
	const uuid = 'FFFFFFFF-0000-4000-8000-00000000000F';
	mounted.set(uuid, stickLibrary(uuid, 'SLOW'));
	const release = holdReads(uuid);
	store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: true }]);
	const read = store.ensureUsbLibrary(uuid);
	await settle(20);
	store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: false }]); // unplugged mid-read
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
	store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: false }]);
	assert.ok(pane.rows.every((r) => r.file_exists === false));

	await store.ensureUsbLibrary(uuid); // the tree remount wins the race
	const changed = store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: true }]);
	assert.deepEqual(changed, [uuid]);
	await waitFor(() => pane.rows.some((r) => r.file_exists === true), 'the replugged pane to refresh');
	assert.ok(pane.rows.every((r) => r.file_exists === true), 'the pane must come back');
	assert.equal(pane.title, 'ORDER / Warmup');
});

test('a dead read settling late does not orphan the live one', async () => {
	const uuid = '88888888-0000-4000-8000-000000000008';
	mounted.set(uuid, stickLibrary(uuid, 'LATE'));
	const releaseFirst = holdReads(uuid);
	store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: true }]);
	const first = store.ensureUsbLibrary(uuid).catch((exc) => exc);
	// The server binds a read to the hold current when it ARRIVES, so the first
	// read must be at the server before the second hold replaces the first;
	// otherwise releaseFirst() frees nothing and the test hangs.
	await waitFor(() => libraryHits(uuid) === 1, 'the first read to reach the server');
	store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: false }]); // unplug: the first read is now dead
	store.applyUsbVolumePresence([{ id: `vol:${uuid}`, present: true }]); // replug
	const releaseSecond = holdReads(uuid);
	const second = store.ensureUsbLibrary(uuid);
	await waitFor(() => libraryHits(uuid) === 2, 'the second read to reach the server');
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

test('a pane switched away during a replug refresh keeps its new list', async () => {
	const uuid = '55555555-0000-4000-8000-000000000005';
	mounted.set(uuid, stickLibrary(uuid, 'SWITCH'));
	const panes = [];
	const pane = await loadedPane(uuid, 'pl-1', panes);
	await daemonLists(uuid);
	await daemonLists(); // unplugged: grayed, cache dropped
	assert.ok(pane.rows.every((r) => r.file_exists === false));

	// Replug, but hold the re-read the watcher starts.
	const release = holdReads(uuid);
	await daemonLists(uuid);
	await settle(20);
	// The DJ switches this pane to a library list while the stick re-reads.
	const seq = pane.beginLoad('pl-9', 'Library list', 'playlist');
	assert.ok(pane.completeLoad(seq, [LIBRARY_ROW], false));
	release();
	// Join the held re-read so the late refresh has really landed before the
	// pane is checked; a fixed sleep could check too early and pass vacuously.
	await store.ensureUsbLibrary(uuid);
	await settle();
	assert.equal(pane.playlist_id, 'pl-9');
	assert.equal(pane.title, 'Library list');
	assert.deepEqual(
		pane.rows.map((r) => r.stable_id),
		[LIBRARY_ROW.stable_id],
		'the late stick refresh must not paint over the list the DJ switched to'
	);
});

test('an unplug leaves a loading pane to its own load, which fails in plain words', async () => {
	const uuid = '44444444-0000-4000-8000-000000000004';
	mounted.set(uuid, stickLibrary(uuid, 'MIDLOAD'));
	await daemonLists(uuid);
	const release = holdReads(uuid);
	const panes = [];
	const pane = new PaneStore();
	panes.push(pane);
	const seq = pane.beginLoad(`usbpl:${uuid}:all`, 'Loading list', 'usb');
	const loading = store.loadUsbPane(pane, seq, () => panes);
	await settle(20);
	await daemonLists(); // pulled while the first read is in flight
	assert.equal(pane.loading, true);
	assert.equal(pane.title, 'Loading list', 'an unplug must not rewrite a pane mid-load');
	release();
	assert.equal(await loading, 'Stick removed');
	assert.equal(pane.error, 'Stick removed');
	assert.equal(pane.title, 'Loading list');
});

test('a stick pane opened before the tracker lists the stick still loads', async () => {
	// An agent (browser_select_playlist with a usbpl: id) or a back-navigation
	// can open a stick pane before any poll has listed that stick. The server
	// has it mounted; the watcher runs mid-read with a list that lacks it.
	const uuid = '33333333-0000-4000-8000-000000000003';
	mounted.set(uuid, stickLibrary(uuid, 'EARLY'));
	assert.equal(trackerRow(uuid), undefined, 'precondition: the tracker has never listed it');
	const release = holdReads(uuid);
	const pane = new PaneStore();
	const seq = pane.beginLoad(`usbpl:${uuid}:all`, 'x', 'usb');
	const loading = store.loadUsbPane(pane, seq, () => [pane]);
	await settle(20);
	runWatcher();
	release();
	assert.equal(await loading, null);
	assert.equal(pane.error, null);
	assert.equal(pane.rows.length, 3);
	assert.equal(libraryHits(uuid), 1, 'one read, not killed and re-read');
});

test('before the first poll this session, restored presence flags are not applied', async () => {
	// Fresh session: the tracker holds last session's list (restored from
	// storage), where this stick was unplugged, and has not polled yet.
	const uuid = '22222222-0000-4000-8000-000000000002';
	mounted.set(uuid, stickLibrary(uuid, 'STALE'));
	const polled = store.usbTracker.scannedAt;
	const volumes = store.usbTracker.volumes;
	store.usbTracker.scannedAt = null;
	store.usbTracker.volumes = [
		{ id: `vol:${uuid}`, name: 'STALE', first_seen: 1, last_seen: 1, present: false }
	];
	try {
		const release = holdReads(uuid);
		const pane = new PaneStore();
		const seq = pane.beginLoad(`usbpl:${uuid}:all`, 'x', 'usb');
		const loading = store.loadUsbPane(pane, seq, () => [pane]);
		await settle(20);
		runWatcher();
		release();
		assert.equal(await loading, null, 'a stale present:false must not kill the read');
		assert.equal(pane.rows.length, 3);
	} finally {
		store.usbTracker.scannedAt = polled;
		store.usbTracker.volumes = volumes;
	}
	// Control: once a poll lands, the same present:false IS an unplug.
	store.usbTracker.volumes = [
		...volumes,
		{ id: `vol:${uuid}`, name: 'STALE', first_seen: 1, last_seen: 1, present: false }
	];
	runWatcher();
	assert.equal(store.usbLibrary.sticks[`vol:${uuid}`], undefined);
	store.usbTracker.volumes = volumes;
});

test('a stick whose export strands a playlist still opens, and All tracks plays', async () => {
	const uuid = '11111111-0000-4000-8000-000000000001';
	const lib = stickLibrary(uuid, 'ODD');
	lib.playlists.push({
		id: 'pl-2',
		pdb_id: 2,
		name: 'Stranded',
		parent_id: 'pl-99',
		is_folder: false,
		sort_order: 1,
		track_ids: [`usb-${uuid}-2`]
	});
	mounted.set(uuid, lib);
	store.openUsbStick({ id: `vol:${uuid}` });
	await store.ensureUsbLibrary(uuid); // joins the read openUsbStick started
	const view = store.usbLibrary.sticks[`vol:${uuid}`];
	assert.equal(view.status, 'ready', `view: ${JSON.stringify(view)}`);
	assert.deepEqual(
		view.tree.map((n) => n.name),
		['All tracks', 'Warmup', 'Stranded']
	);
	const panes = [];
	assert.equal((await loadedPane(uuid, 'all', panes)).rows.length, 3);
	assert.deepEqual(
		(await loadedPane(uuid, 'pl-2', panes)).rows.map((r) => r.stable_id),
		[`usb-${uuid}-2`]
	);
});

test('the presence watcher is armed once, and it feeds the tracker list in', async () => {
	// Every open and pane load above called watchUsbVolumes; one effect only.
	assert.equal(effects.length, 1, `watcher effects registered: ${effects.length}`);
	await store.ensureUsbLibrary(UUID_B);
	await daemonLists(UUID_B);
	assert.equal(store.usbLibrary.sticks[`vol:${UUID_B}`].status, 'ready');
	// The daemon stops listing B; the real poll marks it present:false and the
	// registered effect must read that list and drop stick B.
	await daemonLists();
	assert.equal(trackerRow(UUID_B).present, false);
	assert.equal(store.usbLibrary.sticks[`vol:${UUID_B}`], undefined);
});
