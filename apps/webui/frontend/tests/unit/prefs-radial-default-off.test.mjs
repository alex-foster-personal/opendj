// [if] the real daemon refuses a malformed disk value (422) [then] the migration keeps its claim and nothing is written, [else stop].
//
// Exercises the REAL ui-prefs FastAPI route (AGENTS.md "No mocks and locked
// real fixtures", Sol P1 on PR #6829): a throwaway daemon
// (scripts/testing/midi_maps_test_server.py) serves a temp data dir this file
// owns. Disk state is seeded by writing the real state/ui-prefs.json the route
// reads, and read back the same way. `globalThis.fetch` is wrapped only to
// record the PUT bodies the frontend sends and, for the failed-write phases,
// to aim PUTs at a loopback port nothing listens on, so the failure is a real
// ECONNREFUSED. It never fabricates a response body or status.
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
// Cold import of the server app can be slow on a loaded host; startup tolerance only.
const START_TIMEOUT_MS = 60_000;
/** Wall-clock cap for held-GET to observe PUT ack (CI hosts can exceed 2 s). */
const HELD_GET_ACK_TIMEOUT_MS = 20_000;

const realFetch = globalThis.fetch;
let backendProc;
let apiOrigin;
let refusedOrigin;
let dataDir;
let prefsFile;
let writesRefused = false;
let inFlight = 0;
let migrationPutMustWaitForGet = false;
let migrationGetReadFromDisk = false;
let migrationPutAcknowledged = false;
let migrationPutProcessed = null;
/** Resolvers waiting for `inFlight` to reach 0 (drain in `settle()`). */
const idleWaiters = [];
/** Runs while a held GET waits, before its response is handed back. */
let duringHeldGet = null;
/** PUT bodies the frontend sent since the last `watchWrites()`. */
let puts = [];
/** Statuses of the real GETs since the last `watchWrites()`. */
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

/** A loopback port that was bound and released, so a connect is refused for real. */
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

function armMigrationRace({ putProcessed, whileGetHeld }) {
	migrationPutMustWaitForGet = true;
	migrationGetReadFromDisk = false;
	migrationPutAcknowledged = false;
	migrationPutProcessed = putProcessed;
	duringHeldGet = whileGetHeld;
}

async function recordingFetch(input, init) {
	const request = new Request(input, init);
	inFlight += 1;
	try {
		if (request.method === 'PUT') {
			const body = await request.clone().text();
			puts.push(JSON.parse(body));
			if (migrationPutMustWaitForGet) {
				await waitForOrdering(
					() => migrationGetReadFromDisk,
					'the migration PUT reached disk before the held GET read the legacy value'
				);
			}
			const target = writesRefused ? request.url.replace(apiOrigin, refusedOrigin) : request.url;
			const response = await realFetch(target, { method: 'PUT', headers: request.headers, body });
			if (migrationPutMustWaitForGet && response.ok) migrationPutAcknowledged = true;
			return response;
		}
		const response = await realFetch(request);
		getStatuses.push(response.status);
		if (migrationPutMustWaitForGet) {
			migrationGetReadFromDisk = true;
			const deadline = Date.now() + HELD_GET_ACK_TIMEOUT_MS;
			while (!migrationPutAcknowledged && Date.now() < deadline) {
				await new Promise((resolve) => setTimeout(resolve, 5));
			}
			assert.ok(migrationPutAcknowledged, 'the held GET never saw the migration PUT acknowledged');
			if (migrationPutProcessed) {
				await waitForOrdering(
					migrationPutProcessed,
					'the product did not process the migration PUT acknowledgement while the GET was held'
				);
			}
			for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
			duringHeldGet?.();
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
	dataDir = mkdtempSync(join(tmpdir(), 'mdt-radial-prefs-'));
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
	migrationPutMustWaitForGet = false;
	migrationGetReadFromDisk = false;
	migrationPutAcknowledged = false;
	migrationPutProcessed = null;
	duringHeldGet = null;
	idleWaiters.length = 0;
});

/** Start a phase: what the daemon's disk holds (undefined = no file), whether
 * writes reach it, and a fresh record of what the frontend sends. */
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

const diskRadial = () => JSON.parse(readFileSync(prefsFile, 'utf8')).jog_radial_waveform;

/** Let the boot-time daemon write and its acknowledgement settle. */
async function settle() {
	const deadline = Date.now() + 30_000;

	/** Wait for `inFlight` to hit 0, or assert the hang message at `deadline` if still stuck. */
	async function whenIdleBeforeDeadline() {
		const ms = deadline - Date.now();
		if (ms <= 0) {
			assert.equal(inFlight, 0, 'a real request was still in flight after settling');
			return;
		}
		return new Promise((resolve, reject) => {
			let done = false;
			let hangTimer;
			const cleanup = () => {
				if (hangTimer !== undefined) {
					clearTimeout(hangTimer);
					hangTimer = undefined;
				}
			};
			hangTimer = setTimeout(() => {
				if (done) return;
				done = true;
				cleanup();
				try {
					assert.equal(inFlight, 0, 'a real request was still in flight after settling');
					resolve();
				} catch (err) {
					reject(err);
				}
			}, ms);

			whenIdle()
				.then(() => {
					if (done) return;
					done = true;
					cleanup();
					resolve();
				})
				.catch(reject);
		});
	}

	for (;;) {
		if (Date.now() >= deadline) {
			assert.equal(inFlight, 0, 'a real request was still in flight after settling');
			return;
		}
		while (inFlight > 0) {
			await whenIdleBeforeDeadline();
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

/** A full blob a real build wrote, rewritten to `fields` (undefined deletes a key). */
async function storedBlob(fields) {
	const store = installBrowser();
	seedDisk(undefined);
	watchWrites(); // this fresh boot's own writes land here, not in the caller's record
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

test('R9: a v1 blob with the radial waveform on boots off and is stamped v2', async () => {
	const store = await storedBlob({
		defaults_migration: 1,
		jog_radial_waveform: true,
		ui_skin: 'default'
	});
	const prefs = await loadPrefs();
	await settle();
	assert.equal(prefs.PREFS_DEFAULTS_MIGRATION, 2);
	assert.equal(
		prefs.uiPrefs.jog_radial_waveform,
		false,
		'the old persisted true survived the migration'
	);
	const saved = JSON.parse(store.get(STORAGE_KEY));
	assert.equal(saved.defaults_migration, 2);
	assert.equal(saved.jog_radial_waveform, false);
	assert.equal(
		prefs.uiPrefs.ui_skin,
		'default',
		'migration 1 re-ran on a v1 blob and reset an explicit skin'
	);
});

test('R9: a v2 blob with the radial waveform on keeps it on', async () => {
	await storedBlob({ defaults_migration: 2, jog_radial_waveform: true });
	const prefs = await loadPrefs();
	await settle();
	assert.equal(
		prefs.uiPrefs.jog_radial_waveform,
		true,
		'an explicit pick after the migration was reset'
	);
});

test('R9: a blob with no version gets both migrations', async () => {
	const store = await storedBlob({
		defaults_migration: undefined,
		jog_radial_waveform: true,
		ui_skin: 'default',
		waveform_design: 'tri-band'
	});
	const prefs = await loadPrefs();
	await settle();
	assert.equal(prefs.uiPrefs.jog_radial_waveform, false);
	assert.equal(prefs.uiPrefs.ui_skin, 'mono-dev', 'migration 1 did not run on an unversioned blob');
	assert.equal(prefs.uiPrefs.waveform_design, 'auto');
	assert.equal(JSON.parse(store.get(STORAGE_KEY)).defaults_migration, 2);
});

test('R9: an unacknowledged daemon write keeps the radial value superseded and is resent next boot', async () => {
	const store = await storedBlob({ defaults_migration: 1, jog_radial_waveform: true });
	seedDisk({ jog_radial_waveform: true });
	const failed = watchWrites({ refused: true });
	const reported = [];
	const radialReports = () =>
		reported.filter((line) => line.includes('radial waveform default')).length;
	const originalError = console.error;
	console.error = (...args) => void reported.push(String(args[0]));
	try {
		const first = await loadPrefs();
		await settle();
		assert.deepEqual(
			failed.at(-1),
			{ jog_radial_waveform: false },
			'the migration never told the daemon'
		);
		assert.equal(
			JSON.parse(store.get(STORAGE_KEY)).radial_off_pending,
			true,
			'a failed write dropped the marker'
		);
		await first.hydrateConfirmPrefsFromDisk();
		assert.equal(
			first.uiPrefs.jog_radial_waveform,
			false,
			'the stale disk true hydrated over the migration'
		);
		// Both failed settles (boot, then the hydrate's resend) reject a few hops
		// later; wait for their reports, bounded, so none leaks into the next boot.
		for (let i = 0; i < 200 && radialReports() < 2; i += 1) {
			await new Promise((resolve) => setTimeout(resolve, 5));
		}
	} finally {
		console.error = originalError;
	}
	// Sol P1, PR #6829: a failed settle is reported, never swallowed.
	assert.equal(
		radialReports(),
		2,
		`each failed migration write is reported once: ${JSON.stringify(reported)}`
	);

	installBrowser(store);
	const acked = watchWrites();
	const ackReports = [];
	console.error = (...args) => void ackReports.push(String(args[0]));
	try {
		await loadPrefs();
		await settle();
		await new Promise((resolve) => setTimeout(resolve, 100));
	} finally {
		console.error = originalError;
	}
	assert.ok(
		!ackReports.some((line) => line.includes('radial waveform default')),
		'an acknowledged write was reported as a failure'
	);
	assert.deepEqual(
		acked,
		[{ jog_radial_waveform: false }],
		'the next boot did not resend the false'
	);
	assert.equal(
		JSON.parse(store.get(STORAGE_KEY)).radial_off_pending,
		false,
		'an acknowledged write kept the marker'
	);
	assert.equal(diskRadial(), false, 'the acknowledged false never reached the daemon disk');
});

test('R9: a first boot with no local blob supersedes a pre-migration disk true', async () => {
	installBrowser();
	seedDisk({ jog_radial_waveform: true });
	const puts = watchWrites({ refused: true });
	const prefs = await loadPrefs();
	await settle();
	assert.deepEqual(puts, [], 'a fresh profile wrote to disk before reading it');
	await prefs.hydrateConfirmPrefsFromDisk();
	await settle();
	assert.equal(
		prefs.uiPrefs.jog_radial_waveform,
		false,
		'the legacy daemon true came back on a fresh profile'
	);
	assert.deepEqual(puts, [{ jog_radial_waveform: false }], 'the stale disk true was not rewritten');
	assert.equal(prefs.uiPrefs.radial_off_pending, true, 'a failed rewrite dropped the marker');
});

test('R9: a fresh profile whose disk already holds false settles with no write', async () => {
	installBrowser();
	seedDisk({ jog_radial_waveform: false });
	const puts = watchWrites();
	const prefs = await loadPrefs();
	await prefs.hydrateConfirmPrefsFromDisk();
	await settle();
	assert.deepEqual(puts, []);
	assert.equal(
		prefs.uiPrefs.radial_off_pending,
		false,
		'an agreeing disk left the marker set forever'
	);
});

test('R9: once the migration is acknowledged, or the DJ picks, the disk value hydrates again', async () => {
	await storedBlob({
		defaults_migration: 2,
		jog_radial_waveform: false,
		radial_off_pending: false
	});
	seedDisk({ jog_radial_waveform: true });
	const puts = watchWrites();
	const settled = await loadPrefs();
	await settle();
	assert.deepEqual(puts, [], 'a settled blob wrote the migration again');
	await settled.hydrateConfirmPrefsFromDisk();
	assert.equal(settled.uiPrefs.jog_radial_waveform, true, 'a later disk pick was ignored forever');

	const store = await storedBlob({
		defaults_migration: 2,
		jog_radial_waveform: false,
		radial_off_pending: true
	});
	seedDisk({ jog_radial_waveform: true });
	watchWrites({ refused: true });
	const picked = await loadPrefs();
	await settle();
	picked.setJogRadialWaveform(false);
	assert.equal(
		JSON.parse(store.get(STORAGE_KEY)).radial_off_pending,
		false,
		"the DJ's pick left the marker set"
	);
	// The disk value is the DJ's own business again (the seeded true, to see it land).
	await picked.hydrateConfirmPrefsFromDisk();
	assert.equal(
		picked.uiPrefs.jog_radial_waveform,
		true,
		'the marker still blocked disk after the pick'
	);
});

// Sol P1, PR #6829: only false or absence agrees with the migration. The real
// daemon refuses a non-boolean on disk with a 422 (ui_prefs.py), so a malformed
// value can never hydrate: the marker survives it and nothing is written. Since
// main's PR #7483 the hydrator has no blanket catch, so the refusal rejects to the
// boot path instead of passing silently.
test('R9: a malformed disk radial value is refused by the daemon and keeps the migration claim', async () => {
	for (const bad of ['true', 1, { on: true }, null]) {
		installBrowser();
		seedDisk({ jog_radial_waveform: bad });
		const puts = watchWrites();
		const prefs = await loadPrefs();
		await assert.rejects(
			() => prefs.hydrateConfirmPrefsFromDisk(),
			/jog_radial_waveform must be a boolean/,
			`a malformed ${JSON.stringify(bad)} hydrated without saying so`
		);
		await settle();
		assert.deepEqual(
			getStatuses,
			[422],
			`the daemon served a malformed ${JSON.stringify(bad)} instead of refusing it`
		);
		assert.equal(
			prefs.uiPrefs.radial_off_pending,
			true,
			`a malformed ${JSON.stringify(bad)} cleared the marker`
		);
		assert.equal(
			prefs.uiPrefs.jog_radial_waveform,
			false,
			`a malformed ${JSON.stringify(bad)} turned the radial waveform on`
		);
		assert.deepEqual(puts, [], `a malformed ${JSON.stringify(bad)} was answered with a write`);
	}
	// Control: no file on disk, so the daemon serves its default false, which
	// agrees with the migration and clears the marker quietly.
	installBrowser();
	seedDisk(undefined);
	const puts = watchWrites();
	const agreeing = await loadPrefs();
	await agreeing.hydrateConfirmPrefsFromDisk();
	await settle();
	assert.deepEqual(getStatuses, [200]);
	assert.equal(
		agreeing.uiPrefs.radial_off_pending,
		false,
		'an absent disk value left the marker set'
	);
	assert.deepEqual(puts, []);
});

// Codex P1, PR #6829: the boot's migration PUT and the layout's GET race. A GET
// that read the legacy true before the PUT landed must not hydrate it just
// because the acknowledgement cleared the claim while the response was in flight.
test('R9: a GET that read disk before the migration write landed cannot hydrate the old true', async () => {
	const store = await storedBlob({ defaults_migration: 1, jog_radial_waveform: true });
	seedDisk({ jog_radial_waveform: true });
	const puts = watchWrites();
	let markerWhenHeld;
	armMigrationRace({
		putProcessed: () => JSON.parse(store.get(STORAGE_KEY)).radial_off_pending === false,
		whileGetHeld: () => {
			markerWhenHeld = JSON.parse(store.get(STORAGE_KEY)).radial_off_pending;
		}
	});
	const prefs = await loadPrefs();
	// No settle: the layout starts the GET while the boot PUT is still queued.
	await prefs.hydrateConfirmPrefsFromDisk();
	await settle();
	assert.equal(
		markerWhenHeld,
		false,
		'the race was not reproduced: the PUT had not cleared the claim mid-GET'
	);
	assert.equal(
		prefs.uiPrefs.jog_radial_waveform,
		false,
		'the pre-migration disk true hydrated over the migration'
	);
	assert.ok(
		puts.length >= 1 && puts.every((p) => p.jog_radial_waveform === false),
		JSON.stringify(puts)
	);
	assert.equal(diskRadial(), false);
});

// Overshoot control: a claim held at request time must not override the DJ's
// own pick made while that GET was in flight.
test('R9: a DJ pick made while a claimed GET is in flight stands', async () => {
	const store = await storedBlob({ defaults_migration: 1, jog_radial_waveform: true });
	// Disk true, so a claim that ignored the pick would queue a false behind it.
	seedDisk({ jog_radial_waveform: true });
	const puts = watchWrites();
	let prefs;
	armMigrationRace({
		putProcessed: () => JSON.parse(store.get(STORAGE_KEY)).radial_off_pending === false,
		whileGetHeld: () => prefs.setJogRadialWaveform(true)
	});
	prefs = await loadPrefs();
	await prefs.hydrateConfirmPrefsFromDisk();
	await settle();
	assert.equal(
		prefs.uiPrefs.jog_radial_waveform,
		true,
		"a disk read sent before the DJ's pick overrode it"
	);
	assert.deepEqual(puts.at(-1), { jog_radial_waveform: true }, "the DJ's pick did not land last");
	assert.equal(diskRadial(), true);
});
