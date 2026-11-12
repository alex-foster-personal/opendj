// [if] AutoPlay hydration disagrees with a local or in-flight write [then] the local value stands, [else stop].
//
// Exercises the REAL ui-prefs FastAPI route: a throwaway daemon serves a temp
// data dir this file owns. No fabricated response bodies or statuses.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { after, afterEach, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { midiMapsFixtureLaunch } from './midi-maps-test-server.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPO_ROOT = resolve(FRONTEND_ROOT, '../../..');
const START_TIMEOUT_MS = 60_000;
const HELD_GET_ACK_TIMEOUT_MS = 20_000;

const realFetch = globalThis.fetch;
let backendProc;
let apiOrigin;
let refusedOrigin;
let dataDir;
let prefsFile;
let writesRefused = false;
let inFlight = 0;
let heldPutMustWaitForGet = false;
let heldGetReadFromDisk = false;
let heldPutAcknowledged = false;
let heldPutProcessed = null;
const idleWaiters = [];
let duringHeldGet = null;
let puts = [];
let getStatuses = [];

function startBackend() {
	return new Promise((resolvePort, reject) => {
		const launch = midiMapsFixtureLaunch(REPO_ROOT, ['--data-dir', dataDir]);
		const proc = spawn(launch.command, launch.args, {
			cwd: launch.cwd,
			env: launch.env,
			stdio: ['ignore', 'pipe', 'pipe']
		});
		const timer = setTimeout(() => {
			proc.kill();
			reject(new Error(`test server did not report LISTENING within ${START_TIMEOUT_MS}ms`));
		}, START_TIMEOUT_MS);
		let stderr = '';
		proc.stderr.on('data', (chunk) => {
			stderr += String(chunk);
		});
		proc.on('exit', (code) => {
			if (code !== null && code !== 0) {
				clearTimeout(timer);
				reject(new Error(`test server exited ${code}:\n${stderr}`));
			}
		});
		const lines = createInterface({ input: proc.stdout });
		lines.on('line', (line) => {
			const match = /^LISTENING (\d+)$/.exec(line);
			if (match) {
				clearTimeout(timer);
				lines.close();
				backendProc = proc;
				resolvePort(Number(match[1]));
			}
		});
	});
}

function closedPort() {
	return new Promise((resolvePort, reject) => {
		const probe = createServer();
		probe.on('error', reject);
		probe.listen(0, '127.0.0.1', () => {
			const { port } = probe.address();
			probe.close(() => resolvePort(port));
		});
	});
}

async function waitForOrdering(predicate, message) {
	for (let i = 0; i < 400; i += 1) {
		if (predicate()) return;
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
	assert.ok(predicate(), message);
}

function armHeldGetRace({ putProcessed, whileGetHeld }) {
	heldPutMustWaitForGet = true;
	heldGetReadFromDisk = false;
	heldPutAcknowledged = false;
	heldPutProcessed = putProcessed;
	duringHeldGet = whileGetHeld;
}

async function recordingFetch(input, init) {
	const request = new Request(input, init);
	inFlight += 1;
	try {
		if (request.method === 'PUT') {
			const body = await request.clone().text();
			puts.push(JSON.parse(body));
			if (heldPutMustWaitForGet) {
				await waitForOrdering(
					() => heldGetReadFromDisk,
					'the AutoPlay PUT reached disk before the held GET read the legacy value'
				);
			}
			const target = writesRefused ? request.url.replace(apiOrigin, refusedOrigin) : request.url;
			const response = await realFetch(target, { method: 'PUT', headers: request.headers, body });
			if (heldPutMustWaitForGet && response.ok) heldPutAcknowledged = true;
			return response;
		}
		const response = await realFetch(request);
		getStatuses.push(response.status);
		if (heldPutMustWaitForGet) {
			heldGetReadFromDisk = true;
			duringHeldGet?.();
			const deadline = Date.now() + HELD_GET_ACK_TIMEOUT_MS;
			while (!heldPutAcknowledged && Date.now() < deadline) {
				await new Promise((resolve) => setTimeout(resolve, 5));
			}
			assert.ok(heldPutAcknowledged, 'the held GET never saw the AutoPlay PUT acknowledged');
			if (heldPutProcessed) {
				await waitForOrdering(
					heldPutProcessed,
					'the product did not process the AutoPlay PUT acknowledgement while the GET was held'
				);
			}
			for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
		}
		return response;
	} finally {
		inFlight -= 1;
		notifyIdle();
	}
}

function notifyIdle() {
	if (inFlight !== 0) return;
	for (const wake of idleWaiters.splice(0)) wake();
}

function whenIdle() {
	if (inFlight === 0) return Promise.resolve();
	return new Promise((resolve) => idleWaiters.push(resolve));
}

before(async () => {
	dataDir = mkdtempSync(join(tmpdir(), 'mdt-autoplay-prefs-'));
	mkdirSync(join(dataDir, 'state'));
	prefsFile = join(dataDir, 'state', 'ui-prefs.json');
	apiOrigin = `http://127.0.0.1:${await startBackend()}`;
	refusedOrigin = `http://127.0.0.1:${await closedPort()}`;
	globalThis.fetch = recordingFetch;
});

after(() => {
	globalThis.fetch = realFetch;
	backendProc?.kill();
	rmSync(dataDir, { recursive: true, force: true });
});

afterEach(() => {
	delete globalThis.window;
	delete globalThis.document;
	writesRefused = false;
	heldPutMustWaitForGet = false;
	heldGetReadFromDisk = false;
	heldPutAcknowledged = false;
	heldPutProcessed = null;
	duringHeldGet = null;
	idleWaiters.length = 0;
});

function watchWrites({ refused = false } = {}) {
	writesRefused = refused;
	puts = [];
	getStatuses = [];
	return puts;
}

function seedDisk(prefs) {
	if (prefs === undefined) rmSync(prefsFile, { force: true });
	else writeFileSync(prefsFile, JSON.stringify(prefs));
}

const diskAutoPlay = () => JSON.parse(readFileSync(prefsFile, 'utf8')).auto_play_enabled;

async function settle() {
	const deadline = Date.now() + 30_000;
	for (;;) {
		if (Date.now() >= deadline) {
			assert.equal(inFlight, 0, 'a real request was still in flight after settling');
			return;
		}
		while (inFlight > 0) {
			await whenIdle();
			if (Date.now() >= deadline) {
				assert.equal(inFlight, 0, 'a real request was still in flight after settling');
				return;
			}
		}
		let quiet = 0;
		while (quiet < 20) {
			if (Date.now() >= deadline) {
				assert.equal(inFlight, 0, 'a real request was still in flight after settling');
				return;
			}
			await new Promise((resolve) => setTimeout(resolve, 0));
			if (inFlight > 0) {
				quiet = 0;
				break;
			}
			quiet += 1;
		}
		if (quiet === 20) {
			assert.equal(inFlight, 0, 'a real request was still in flight after settling');
			return;
		}
	}
}

function installBrowser(store = new Map()) {
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => void store.set(k, String(v)),
			removeItem: (k) => void store.delete(k)
		}
	};
	globalThis.document = { documentElement: { dataset: {}, style: {} } };
	return store;
}

const loadPrefs = () =>
	loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: apiOrigin });

const autoPlayPuts = () => puts.filter((p) => p.auto_play_enabled !== undefined);

test('A: a fresh profile boots AutoPlay off with no disk write', async () => {
	installBrowser();
	seedDisk(undefined);
	const sent = watchWrites();
	const prefs = await loadPrefs();
	await prefs.hydrateConfirmPrefsFromDisk();
	await settle();
	assert.equal(prefs.uiPrefs.auto_play_enabled, false);
	assert.equal(prefs.uiPrefs.auto_play_unsaved, false);
	assert.deepEqual(autoPlayPuts(), []);
	assert.deepEqual(
		sent.filter((p) => p.auto_play_enabled !== undefined),
		[]
	);
});

test('B: offline AutoPlay ON survives reload and resends until acknowledged', async () => {
	const store = installBrowser();
	seedDisk(undefined);
	const failed = watchWrites({ refused: true });
	const reported = [];
	const autoPlayReports = () =>
		reported.filter((line) => line.includes('Could not save AutoPlay to disk')).length;
	const originalError = console.error;
	console.error = (...args) => void reported.push(String(args[0]));
	try {
		const first = await loadPrefs();
		await settle();
		first.setAutoPlayEnabled(true);
		await settle();
		assert.equal(autoPlayReports(), 1, 'the first failed save is reported');
		assert.equal(JSON.parse(store.get(STORAGE_KEY)).auto_play_unsaved, true);
		await first.hydrateConfirmPrefsFromDisk();
		assert.equal(first.uiPrefs.auto_play_enabled, true, 'disk default false clobbered local ON');
		for (let i = 0; i < 200 && autoPlayReports() < 1; i += 1) {
			await new Promise((resolve) => setTimeout(resolve, 5));
		}
	} finally {
		console.error = originalError;
	}
	assert.equal(autoPlayReports(), 1, 'the failed save is reported once');

	installBrowser(store);
	const acked = watchWrites();
	const ackReports = [];
	console.error = (...args) => void ackReports.push(String(args[0]));
	try {
		const second = await loadPrefs();
		await second.hydrateConfirmPrefsFromDisk();
		await settle();
		await new Promise((resolve) => setTimeout(resolve, 100));
		assert.equal(second.uiPrefs.auto_play_enabled, true, 'boot resend did not restore ON');
		assert.deepEqual(
			acked.filter((p) => p.auto_play_enabled === true),
			[{ auto_play_enabled: true }],
			'the boot resend never reached disk'
		);
		assert.equal(JSON.parse(store.get(STORAGE_KEY)).auto_play_unsaved, false);
		assert.equal(diskAutoPlay(), true);
	} finally {
		console.error = originalError;
	}
	assert.ok(
		!ackReports.some((line) => line.includes('Could not save AutoPlay to disk')),
		'an acknowledged write was reported as failure'
	);
});

test('C: a local AutoPlay write during hydration beats disk false (Trackify race)', async () => {
	installBrowser();
	seedDisk({ auto_play_enabled: false });
	watchWrites();
	const prefs = await loadPrefs();
	await settle();
	const hydrate = prefs.hydrateConfirmPrefsFromDisk();
	prefs.setAutoPlayEnabled(true);
	await hydrate;
	await settle();
	assert.equal(prefs.uiPrefs.auto_play_enabled, true);
	const store = globalThis.window.localStorage;
	const saved = JSON.parse(store.getItem(STORAGE_KEY));
	assert.equal(saved.auto_play_enabled, true);
});

test('C2: a held GET answered after AutoPlay PUT ack still cannot clobber local ON', async () => {
	const store = installBrowser();
	seedDisk({ auto_play_enabled: false });
	watchWrites();
	let markerWhenHeld;
	const prefs = await loadPrefs();
	await settle();
	armHeldGetRace({
		putProcessed: () => {
			markerWhenHeld = JSON.parse(store.get(STORAGE_KEY)).auto_play_unsaved === false;
			return markerWhenHeld;
		},
		whileGetHeld: () => prefs.setAutoPlayEnabled(true)
	});
	await prefs.hydrateConfirmPrefsFromDisk();
	await settle();
	assert.equal(
		markerWhenHeld,
		true,
		'the revision-guard race was not reproduced: PUT had not cleared before body apply'
	);
	assert.equal(prefs.uiPrefs.auto_play_enabled, true, 'disk false hydrated over Trackify ON');
});

async function storedBlob(fields) {
	const store = installBrowser();
	seedDisk(undefined);
	watchWrites();
	const first = await loadPrefs();
	await settle();
	first.setUiSkin('light');
	await settle();
	const blob = JSON.parse(store.get(STORAGE_KEY));
	for (const [key, value] of Object.entries(fields)) {
		if (value === undefined) delete blob[key];
		else blob[key] = value;
	}
	store.set(STORAGE_KEY, JSON.stringify(blob));
	installBrowser(store);
	return store;
}

test('D: after acknowledgement, a later disk true hydrates over local off', async () => {
	await storedBlob({
		auto_play_enabled: false,
		auto_play_unsaved: false
	});
	seedDisk({ auto_play_enabled: true });
	watchWrites();
	const prefs = await loadPrefs();
	await prefs.hydrateConfirmPrefsFromDisk();
	await settle();
	assert.equal(prefs.uiPrefs.auto_play_enabled, true, 'another browser ON was ignored');
});
