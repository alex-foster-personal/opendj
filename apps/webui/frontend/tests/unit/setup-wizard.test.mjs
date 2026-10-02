/**
 * Contract tests for the first-run wizard (src/lib/setup/*).
 *
 * One seam: globalThis.fetch. The client under test is the REAL generated
 * one, so these exercise the actual URL building, the throwing middleware and
 * the error decoding rather than a stub of them -- the idiom
 * CONVERSION-PATTERN.md prescribes and jobs-store.test.mjs already follows.
 *
 * Regression lines:
 * - if the step rules stop refusing on a fatal blocker then Next walks a user
 *   into an import that cannot run
 * - if a rekordbox blocker also blocks the FOLDER branch then the whole
 *   no-rekordbox path is dead
 * - if beginImport advances the step on a refusal then the wizard shows a
 *   progress bar for a job that was never queued
 * - if a legacy boot issues a setup request at all then it is a guaranteed
 *   404 that teaches the user nothing
 * - if the access caveat is dropped then a count taken behind a permission
 *   wall travels without saying so
 * - if finish() closes on the BOOT preflight reading then the root layout
 *   re-raises setup the moment it closes and Start playing does nothing
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { engineHealth, jsonResponse } from './setup-fixtures.mjs';

const API_BASE = 'https://setup.example.test';

let mod;
let wizard;
/** The singleton's source as CONSTRUCTED, read before any _resetForTests. */
let constructedSource;
let originalFetch;
let requests;

function fileProbe(path, exists = true) {
	return { path, exists, size_bytes: exists ? 1024 : null, modified_at: null };
}

function detection(overrides = {}) {
	return {
		installed: true,
		live_db: fileProbe('/Users/dj/Library/Pioneer/rekordbox/master.db'),
		share_dir: fileProbe('/Users/dj/Library/Pioneer/rekordbox/share'),
		working_copy: fileProbe('/data/master.db.copy', false),
		plain_copy: fileProbe('/data/master.plain.db'),
		key_available: true,
		key_detail: 'pyrekordbox holds a 64-character key',
		import_source: '/data/master.plain.db',
		import_source_encrypted: false,
		blockers: [],
		rekordbox_running: false,
		...overrides
	};
}

function permissions(overrides = {}) {
	return {
		all_readable: true,
		denied: [],
		roots: [
			{
				path: '/Users/dj/Music',
				exists: true,
				readable: true,
				denied: false,
				detail: 'readable'
			}
		],
		how_to_grant: 'Open System Settings > Privacy & Security > Files and Folders',
		...overrides
	};
}

function status(overrides = {}) {
	return {
		library_empty: true,
		tracks: 0,
		playlists: 0,
		state_db: fileProbe('/data/state/state.db', false),
		data_dir: '/data',
		dismissed: false,
		should_show_wizard: true,
		stages: ['detect', 'snapshot', 'decrypt', 'ingest', 'analysis'],
		folder_stages: ['detect', 'scan', 'ingest'],
		last_import: null,
		rekordbox: detection(),
		permissions: permissions(),
		...overrides
	};
}

function folderScan(overrides = {}) {
	return {
		path: '/Users/dj/Music',
		exists: true,
		readable: true,
		denied: false,
		detail: 'readable',
		audio_files: 12,
		icloud_placeholders: 0,
		how_to_grant: 'Open System Settings > Privacy & Security > Files and Folders',
		sample: ['/Users/dj/Music/a.wav'],
		...overrides
	};
}

function folderRow(path, scan = null, id = 'row-1') {
	return { id, path, scan };
}

function emptyFolderRows() {
	return [folderRow('', null)];
}

function job(overrides = {}) {
	return {
		id: 'job-setup-1',
		kind: 'setup.import-rekordbox',
		payload: {},
		status: 'queued',
		progress: 0,
		message: null,
		error: null,
		attempt: 1,
		created_at: '2026-08-19T10:00:00.000Z',
		started_at: null,
		finished_at: null,
		owner_pid: 1,
		owner_boot_id: 'boot-1',
		worker_pid: null,
		worker_pgid: null,
		worker_argv: null,
		worker_started_at: null,
		external_ref: null,
		...overrides
	};
}

/** Record every request and answer from a path -> body map. */
function routeFetch(routes) {
	globalThis.fetch = async (request) => {
		requests.push(request);
		const url = new URL(request.url);
		const handler = routes[url.pathname];
		if (handler === undefined) {
			return jsonResponse({ detail: `no route for ${url.pathname}` }, 404);
		}
		return typeof handler === 'function' ? handler(request, url) : jsonResponse(handler);
	};
}

before(async () => {
	mod = await loadTypeScriptModule('tests/unit/fixtures/setup-wizard-entry.ts', {
		viteApiBase: API_BASE
	});
	wizard = mod.setupWizard;
	constructedSource = wizard.source;
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () => jsonResponse(engineHealth());
	assert.equal(await mod.capabilities.probe(), 'engine');
	globalThis.fetch = originalFetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	wizard._resetForTests();
	mod._resetPreflightForTests();
	requests = [];
});

// ---------------------------------------------------------------- step rules

test('the steps run welcome -> detect -> confirm -> progress -> stems -> done', () => {
	assert.deepEqual(mod.WIZARD_STEPS, [
		'welcome',
		'detect',
		'confirm',
		'progress',
		'stems',
		'done'
	]);
});

test('an unselected source never visits confirm', () => {
	const route = mod.visibleSteps(null);
	assert.ok(!route.includes('confirm'), 'confirm requires an explicit rekordbox choice');
});

test('selecting rekordbox explicitly restores the confirm route', () => {
	const route = mod.visibleSteps('rekordbox');
	assert.ok(route.includes('confirm'));
	assert.equal(mod.nextStepFor('detect', 'rekordbox'), 'confirm');
});

test('next and previous clamp at both ends rather than falling off', () => {
	assert.equal(mod.previousStep('welcome'), 'welcome');
	assert.equal(mod.nextStep('done'), 'done');
	assert.equal(mod.nextStep('welcome'), 'detect');
	assert.equal(mod.previousStep('done'), 'stems');
});

test('detect refuses Next until a source is chosen', () => {
	const ctx = { source: null, detection: detection(), folderScan: null, job: null };
	assert.match(mod.advanceRefusal('detect', ctx), /choose an import source/);
});

test('detect refuses Next until detection has answered', () => {
	const ctx = { source: 'rekordbox', detection: null, folderRows: emptyFolderRows(), job: null };
	assert.match(mod.advanceRefusal('detect', ctx), /has not answered/);
});

test('a fatal blocker refuses Next and names itself to agents', () => {
	const ctx = {
		source: 'rekordbox',
		detection: detection({ blockers: ['rekordbox_not_found'] }),
		folderRows: emptyFolderRows(),
		job: null
	};
	assert.match(mod.advanceRefusal('detect', ctx), /rekordbox_not_found/);
	assert.match(mod.humanRefusal('detect', ctx), /cannot start yet/i);
	assert.doesNotMatch(mod.humanRefusal('detect', ctx), /rekordbox_not_found/);
});

test('a missing share dir warns but does NOT block the import', () => {
	// Tracks land either way; only the waveforms are missing. Refusing the
	// whole import over it would deny a usable library for a later fix.
	const ctx = {
		source: 'rekordbox',
		detection: detection({ blockers: ['rekordbox_share_missing'] }),
		folderRows: emptyFolderRows(),
		job: null
	};
	assert.equal(mod.advanceRefusal('detect', ctx), null);
	assert.deepEqual(mod.fatalBlockers(ctx.detection), []);
});

test('a rekordbox blocker never blocks the FOLDER branch', () => {
	// The whole point of the no-rekordbox path: someone with no rekordbox at
	// all must still be able to reach a library.
	const ctx = {
		source: 'folder',
		detection: detection({ blockers: ['rekordbox_not_found'] }),
		folderRows: [folderRow('/Users/dj/Music', folderScan())],
		job: null
	};
	assert.equal(mod.advanceRefusal('detect', ctx), null);
});

test('the folder branch refuses a denied folder and says to grant access', () => {
	const ctx = {
		source: 'folder',
		detection: null,
		folderRows: [
			folderRow(
				'/Users/dj/Music',
				folderScan({ denied: true, readable: false, audio_files: 0 })
			)
		],
		job: null
	};
	assert.match(mod.advanceRefusal('detect', ctx), /blocking that folder/);
});

test('the folder branch refuses a readable folder with nothing in it', () => {
	const ctx = {
		source: 'folder',
		detection: null,
		folderRows: [folderRow('/Users/dj/Music', folderScan({ audio_files: 0 }))],
		job: null
	};
	assert.match(mod.advanceRefusal('detect', ctx), /nothing importable/);
});

test('the folder branch allows one importable row and one empty row', () => {
	const ctx = {
		source: 'folder',
		detection: null,
		folderRows: [
			folderRow('/Users/dj/Music', folderScan(), 'row-1'),
			folderRow('', null, 'row-2')
		],
		job: null
	};
	assert.equal(mod.advanceRefusal('detect', ctx), null);
});

test('the folder branch refuses a non-empty row that was not checked', () => {
	const ctx = {
		source: 'folder',
		detection: null,
		folderRows: [
			folderRow('/Users/dj/Music', folderScan(), 'row-1'),
			folderRow('/Users/dj/Other', null, 'row-2')
		],
		job: null
	};
	assert.match(mod.advanceRefusal('detect', ctx), /check \/Users\/dj\/Other first/);
});

test('a checked row whose path was retyped is no longer importable (#3681 P1)', () => {
	// [if] a row checked as one folder is edited to another [then] its old scan
	// no longer vouches for it: Next asks for a check and nothing is imported,
	// [else stop]. bind:value edits row.path in place and leaves row.scan alone.
	const rows = [folderRow('/Users/dj/Other', folderScan({ path: '/Users/dj/Music' }))];
	const ctx = { source: 'folder', detection: null, folderRows: rows, job: null };
	assert.equal(mod.currentFolderScan(rows[0]), null);
	assert.match(mod.advanceRefusal('detect', ctx), /no folder has been checked yet/);
	assert.deepEqual(mod.importableFolderPathsFromRows(rows), []);

	const mixed = [
		folderRow('/Users/dj/Music', folderScan(), 'row-1'),
		folderRow('/Users/dj/Other', folderScan({ path: '/Users/dj/Elsewhere' }), 'row-2')
	];
	assert.match(
		mod.advanceRefusal('detect', { ...ctx, folderRows: mixed }),
		/check \/Users\/dj\/Other first/
	);
	assert.deepEqual(mod.importableFolderPathsFromRows(mixed), ['/Users/dj/Music']);
});

test('an edit that normalizes to the scanned path keeps the scan (#3681 control)', () => {
	// [if] the retyped path differs only by a trailing separator [then] the scan
	// still describes it and the row stays importable, [else stop].
	const rows = [folderRow('/Users/dj/Music/', folderScan())];
	const ctx = { source: 'folder', detection: null, folderRows: rows, job: null };
	assert.equal(mod.advanceRefusal('detect', ctx), null);
	assert.deepEqual(mod.importableFolderPathsFromRows(rows), ['/Users/dj/Music']);
});

test('progress refuses Next while the import is still live', () => {
	const ctx = { source: 'rekordbox', detection: detection(), folderRows: emptyFolderRows() };
	assert.match(
		mod.advanceRefusal('progress', { ...ctx, job: job({ status: 'running' }) }),
		/is running/
	);
	assert.match(
		mod.advanceRefusal('progress', { ...ctx, job: job({ status: 'failed' }) }),
		/re-run it/
	);
	const succeeded = job({ status: 'succeeded', progress: 1 });
	assert.match(
		mod.advanceRefusal('progress', { ...ctx, job: succeeded, statusRefreshJobId: null }),
		/still loading/
	);
	assert.equal(
		mod.advanceRefusal('progress', {
			...ctx,
			job: succeeded,
			statusRefreshJobId: succeeded.id
		}),
		null
	);
});

test('progress surfaces a failed post-import status refresh', () => {
	// [if] the post-import status refresh failed [then] Continue says so,
	// [else stop].
	const ctx = {
		source: 'rekordbox',
		detection: detection(),
		folderRows: emptyFolderRows(),
		job: job({ status: 'succeeded', progress: 1 }),
		statusRefreshJobId: null,
		statusRefreshError: 'state db is gone'
	};
	assert.match(mod.advanceRefusal('progress', ctx), /state db is gone/);
});

test('the operator copy for a pending status re-read never calls a finished import failed', () => {
	// [if] the import succeeded and its status re-read is pending or failed
	// [then] the operator reads that it finished, not "did not finish
	// successfully", and the raw reason stays out of the copy, [else stop].
	const succeeded = job({ status: 'succeeded', progress: 1 });
	const ctx = {
		source: 'folder',
		detection: null,
		folderRows: emptyFolderRows(),
		job: succeeded,
		statusRefreshJobId: null,
		statusRefreshError: null
	};
	const loading = mod.humanRefusal('progress', ctx);
	assert.match(loading, /import finished/i);
	assert.doesNotMatch(loading, /did not finish/);
	const failedRead = mod.humanRefusal('progress', { ...ctx, statusRefreshError: 'GET /api/v1/setup/status 500' });
	assert.match(failedRead, /import finished/i);
	assert.doesNotMatch(failedRead, /\/api\/v1|500/);
	// control: a FAILED import still says it did not finish
	assert.match(mod.humanRefusal('progress', { ...ctx, job: job({ status: 'failed' }) }), /did not finish/);
	// control: once the re-read landed there is no refusal at all
	assert.equal(mod.humanRefusal('progress', { ...ctx, statusRefreshJobId: succeeded.id }), null);
});

test('importPct clamps and rounds rather than trusting the row', () => {
	assert.equal(mod.importPct(null), 0);
	assert.equal(mod.importPct(job({ progress: 0.456 })), 46);
	assert.equal(mod.importPct(job({ progress: 1.4 })), 100);
	assert.equal(mod.importPct(job({ progress: -1 })), 0);
});

// ------------------------------------------------------------------- loading

test('load fills status and detection from one status request', async () => {
	routeFetch({ '/api/v1/setup/status': status() });

	await wizard.load();

	assert.equal(requests.length, 1);
	assert.equal(requests[0].url, `${API_BASE}/api/v1/setup/status`);
	assert.equal(wizard.status.tracks, 0);
	assert.equal(wizard.detection.import_source, '/data/master.plain.db');
	assert.equal(wizard.error, null);
});

test('STANDALONE-08: the wizard is constructed with NO source, before any reset', () => {
	// Mutation guard: _resetForTests() nulls the source before every test, so
	// only the value captured at load time proves the class field itself
	// defaults to null. Reverting the initial state to 'rekordbox' fails here.
	assert.equal(constructedSource, null, 'a freshly built wizard must not assume rekordbox');
});

test('STANDALONE-08: detection alone leaves source unselected and makes no import POST', async () => {
	routeFetch({ '/api/v1/setup/status': status() });

	await wizard.load();

	assert.equal(wizard.source, null, 'detection must not select rekordbox');
	const importPosts = requests.filter(
		(request) => request.method === 'POST' && request.url.includes('/api/v1/setup/import')
	);
	assert.equal(importPosts.length, 0, 'detection alone must not enqueue an import');

	wizard.useSource('rekordbox');
	assert.equal(wizard.source, 'rekordbox', 'the explicit pick is the only way to select it');
	// The import that follows the explicit pick is NOT driven here: a fabricated
	// 202 for /api/v1/setup/import would be simulated API success (AGENTS.md,
	// Codex P1 on #3561). The real endpoint is exercised by the browser test
	// 'STANDALONE-08: rekordbox detection alone does not opt in or import'.
});

test('_resetForTests leaves source unselected', () => {
	wizard.useSource('rekordbox');
	wizard._resetForTests();
	assert.equal(wizard.source, null);
});

const GENERIC = 'Something went wrong talking to the app. Try again in a moment.';
const TOO_SLOW = 'The app took too long to answer. Try again in a moment.';

test('a failed load keeps the server message for agents only, and KEEPS what was on screen', async () => {
	routeFetch({ '/api/v1/setup/status': status() });
	await wizard.load();

	routeFetch({
		'/api/v1/setup/status': () =>
			jsonResponse({ detail: { code: 'boom', message: 'state db is gone' } }, 500)
	});
	await wizard.load();

	assert.equal(wizard.error, GENERIC);
	assert.equal(wizard.errorDiagnostic, 'state db is gone');
	assert.equal(wizard.status.tracks, 0, 'previous status must survive a failure');
});

test('a plain-string status error never reaches the operator word for word (Mac check item 4)', async () => {
	// FastAPI's own layer answers {"detail": "<string>"}; humanApiError passed
	// such a sentence through whenever it carried no internals, so the engine's
	// own words were drawn on Welcome.
	routeFetch({
		'/api/v1/setup/status': () => jsonResponse({ detail: 'the disk said no' }, 500)
	});
	await wizard.load();

	assert.equal(wizard.error, GENERIC);
	assert.equal(wizard.errorDiagnostic, 'the disk said no', 'agents keep the raw words');
	assert.equal(wizard.detectState, 'failed');
});

/** A response that never comes on its own but rejects once the request's
 * signal aborts, the way a browser fetch does. Records that it was aborted. */
function stalled(request, seen) {
	return new Promise((_, reject) => {
		request.signal.addEventListener('abort', () => {
			seen.aborted = true;
			reject(new DOMException('The operation was aborted.', 'AbortError'));
		});
	});
}

/** Fail, instead of hanging the suite, when a call never settles. */
async function settlesWithin(promise, ms, what) {
	let timer;
	const guard = new Promise((_, reject) => {
		timer = setTimeout(() => reject(new Error(`${what} never settled within ${ms} ms`)), ms);
	});
	try {
		return await Promise.race([promise, guard]);
	} finally {
		clearTimeout(timer);
	}
}

test('the setup read deadline defaults to 20 s', () => {
	assert.equal(mod.SETUP_READ_TIMEOUT_MS, 20_000);
	assert.equal(wizard.readTimeoutMs, 20_000);
});

test('a status read that never answers is aborted at the deadline and ends failed (Mac check 3a)', async () => {
	const seen = { aborted: false };
	routeFetch({ '/api/v1/setup/status': (request) => stalled(request, seen) });
	wizard.readTimeoutMs = 30;

	await settlesWithin(wizard.load(), 2_000, 'load()');

	assert.equal(wizard.detectState, 'failed', 'a stall must end, not stay scanning');
	assert.equal(wizard.busy, false, 'Get started must not stay disabled');
	assert.equal(seen.aborted, true, 'the stalled request must be aborted, not left open');
	assert.equal(wizard.error, TOO_SLOW);
	assert.match(wizard.errorDiagnostic, /GET \/api\/v1\/setup\/status gave no answer within 30 ms/);
});

test('control: a read that answers inside the deadline is not cut short', async () => {
	// The overshoot of the deadline fix: a timer that fires anyway, or one
	// shorter than the answer it is waiting for.
	routeFetch({
		'/api/v1/setup/status': () =>
			new Promise((resolve) => setTimeout(() => resolve(jsonResponse(status())), 20))
	});
	wizard.readTimeoutMs = 500;

	await wizard.load();
	await new Promise((resolve) => setTimeout(resolve, 30));

	assert.equal(wizard.detectState, 'answered');
	assert.equal(wizard.error, null);
	assert.equal(wizard.detection.import_source, '/data/master.plain.db');
});

test('a Look again that never answers ends failed and keeps the earlier answer (Mac check 3a/3b)', async () => {
	routeFetch({ '/api/v1/setup/status': status() });
	await wizard.load();

	const seen = { aborted: false };
	routeFetch({ '/api/v1/setup/detect/rekordbox': (request) => stalled(request, seen) });
	wizard.readTimeoutMs = 30;
	await settlesWithin(wizard.redetect(), 2_000, 'redetect()');

	assert.equal(wizard.detectState, 'failed');
	assert.equal(seen.aborted, true);
	assert.equal(wizard.error, TOO_SLOW);
	assert.equal(wizard.detection.import_source, '/data/master.plain.db', 'the earlier answer stays');
});

test('ensureLoaded never re-runs a load the engine answered with a failure (Mac check 3c)', async () => {
	// The overlay drives ensureLoaded() from an effect. Retrying a real
	// failure from there looped load() and held Get started disabled.
	let calls = 0;
	routeFetch({
		'/api/v1/setup/status': () => {
			calls += 1;
			return jsonResponse({ detail: 'down' }, 500);
		}
	});
	await wizard.load();
	assert.equal(calls, 1);

	await wizard.ensureLoaded();
	assert.equal(calls, 1, 'ensureLoaded retried a failed status read on its own');
	assert.equal(wizard.detectState, 'failed');

	// The operator's own retry still asks again.
	await wizard.load();
	assert.equal(calls, 2);
});

test('a failed Look again is not repainted by a status re-read (Mac check 3b)', async () => {
	routeFetch({ '/api/v1/setup/status': status() });
	await wizard.load();

	routeFetch({
		'/api/v1/setup/status': status({ rekordbox: detection({ installed: false }) }),
		'/api/v1/setup/detect/rekordbox': () => jsonResponse({ detail: 'down' }, 500)
	});
	await wizard.redetect();
	await wizard.ensureLoaded();

	assert.equal(wizard.detectState, 'failed', 'the failure must stay on screen');
	assert.equal(wizard.error, GENERIC);
	assert.equal(
		requests.filter((request) => request.url.endsWith('/api/v1/setup/status')).length,
		1,
		'only the first load may read status'
	);
	assert.equal(wizard.detection.installed, true, 'the earlier answer is what stays');
});

test('refreshStatusAfterImport fills last_import once per completed job', async () => {
	// [if] an import job succeeded [then] Done's status is re-read and a
	// second notice does not fetch again, [else stop].
	let statusCalls = 0;
	routeFetch({
		'/api/v1/setup/status': () => {
			statusCalls += 1;
			if (statusCalls === 1) return jsonResponse(status({ last_import: null }));
			return jsonResponse(
				status({
					tracks: 4,
					library_empty: false,
					last_import: {
						kind: 'folder',
						finished_at: '2026-10-02T00:00:00.000Z',
						started_at: '2026-10-02T00:00:00.000Z',
						tracks_written: 4,
						files_seen: 4,
						tracks: 4,
						tracks_without_analysis: 4,
						analysis_detail: 'tags only',
						analysis_available: false,
						files_dataless: 0,
						files_rejected_unplayable: 0,
						files_without_tags: 0,
						unreadable_roots: []
					}
				})
			);
		}
	});
	await wizard.load();
	assert.equal(wizard.status.last_import, null);

	await wizard.refreshStatusAfterImport('job-folder-1');
	assert.equal(wizard.status.last_import.tracks_written, 4);
	assert.equal(wizard.status.tracks, 4);
	assert.equal(statusCalls, 2);

	const before = statusCalls;
	await wizard.refreshStatusAfterImport('job-folder-1');
	assert.equal(statusCalls, before, 'a second completion notice must not re-fetch');
});

test('refreshStatusAfterImport keeps prior status when the server refuses', async () => {
	routeFetch({ '/api/v1/setup/status': status() });
	await wizard.load();

	routeFetch({
		'/api/v1/setup/status': () =>
			jsonResponse({ detail: { code: 'boom', message: 'state db is gone' } }, 500)
	});
	await wizard.refreshStatusAfterImport('job-folder-2');

	assert.equal(wizard.error, 'state db is gone');
	assert.equal(wizard.statusRefreshError, 'state db is gone');
	assert.equal(wizard.status.tracks, 0, 'previous status must survive a failure');
	assert.equal(wizard.statusRefreshJobId, null);
});

test('redetect re-asks the detect endpoint specifically', async () => {
	routeFetch({ '/api/v1/setup/detect/rekordbox': detection({ installed: false }) });

	await wizard.redetect();

	assert.equal(requests[0].url, `${API_BASE}/api/v1/setup/detect/rekordbox`);
	assert.equal(wizard.detection.installed, false);
});

// REQ: SETUP-11
test('loadFolderCandidates fills folderCandidates from the endpoint', async () => {
	routeFetch({
		'/api/v1/setup/detect/music-folders': {
			candidates: [
				{
					path: '/Users/dj/Music',
					exists: true,
					readable: true,
					denied: false,
					detail: 'readable'
				}
			]
		}
	});

	await wizard.loadFolderCandidates();

	assert.equal(requests[0].url, `${API_BASE}/api/v1/setup/detect/music-folders`);
	assert.equal(wizard.folderCandidatesState, 'answered');
	assert.equal(wizard.folderCandidates.length, 1);
	assert.equal(wizard.folderCandidates[0].path, '/Users/dj/Music');
});

test('a failed folder-candidates load sets failed and keeps prior candidates', async () => {
	wizard.folderCandidates = [
		{
			path: '/Users/dj/Music',
			exists: true,
			readable: true,
			denied: false,
			detail: 'readable'
		}
	];
	wizard.folderCandidatesState = 'idle';

	routeFetch({
		'/api/v1/setup/detect/music-folders': () =>
			jsonResponse({ detail: { code: 'boom', message: 'engine offline' } }, 500)
	});
	await wizard.loadFolderCandidates();

	assert.equal(wizard.folderCandidatesState, 'failed');
	assert.equal(wizard.folderCandidates.length, 1, 'previous candidates must survive a failure');
});

test('useSource folder triggers loadFolderCandidates once', async () => {
	routeFetch({
		'/api/v1/setup/detect/music-folders': { candidates: [] }
	});

	wizard.useSource('folder');
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(wizard.folderCandidatesState, 'answered');

	const before = requests.length;
	wizard.useSource('folder');
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(requests.length, before, 'candidates load only once per wizard-open');
});

// ------------------------------------------------------------------- imports

test('beginImport refuses without rekordbox source and makes no import POST', async () => {
	routeFetch({ '/api/v1/setup/import': () => jsonResponse(job(), 202) });

	await wizard.beginImport();

	assert.equal(wizard.jobId, null);
	assert.equal(wizard.step, 'welcome');
	assert.equal(wizard.error, 'Choose to import your DJ collection first.');
	assert.equal(wizard.errorDiagnostic, 'choose rekordbox import before starting');
	assert.equal(
		requests.filter(
			(request) => request.method === 'POST' && request.url.includes('/api/v1/setup/import')
		).length,
		0
	);
});

test('beginImport advances to progress and records the job id', async () => {
	routeFetch({ '/api/v1/setup/import': () => jsonResponse(job(), 202) });
	wizard.useSource('rekordbox');

	await wizard.beginImport();

	assert.equal(wizard.jobId, 'job-setup-1');
	assert.equal(wizard.step, 'progress');
	assert.equal(requests[0].method, 'POST');
});

test('a refused import leaves the step alone and shows the server sentence', async () => {
	routeFetch({
		'/api/v1/setup/import': () =>
			jsonResponse(
				{
					detail: {
						code: 'setup_import_already_running',
						message: 'setup import job-9 is already running'
					}
				},
				409
			)
	});
	wizard.useSource('rekordbox');
	wizard.goTo('confirm');

	await wizard.beginImport();

	assert.equal(wizard.step, 'confirm', 'must not show progress for a job that was refused');
	assert.equal(wizard.jobId, null);
	assert.equal(wizard.error, 'An import is already running. Wait for it to finish, then try again.');
	assert.equal(wizard.errorDiagnostic, 'setup import job-9 is already running');
});

test('refreshDecrypt is sent as the flag the CLI calls --refresh-decrypt', async () => {
	let body;
	routeFetch({
		'/api/v1/setup/import': async (request) => {
			body = await request.clone().json();
			return jsonResponse(job(), 202);
		}
	});
	wizard.useSource('rekordbox');

	await wizard.beginImport({ refreshDecrypt: true });

	assert.deepEqual(body, { refresh_decrypt: true });
});

// REQ: SETUP-11
test('applyFolderSuggestion fills the path and runs detect/folder for that path', async () => {
	routeFetch({ '/api/v1/setup/detect/folder': folderScan() });
	wizard.folderRows = [folderRow('')];

	wizard.applyFolderSuggestion('/Users/dj/Music');
	await new Promise((resolve) => setTimeout(resolve, 0));

	const url = new URL(requests[0].url);
	assert.equal(url.pathname, '/api/v1/setup/detect/folder');
	assert.equal(url.searchParams.get('path'), '/Users/dj/Music');
	assert.equal(wizard.folderRows[0].path, '/Users/dj/Music');
	assert.equal(wizard.folderRows[0].scan.audio_files, 12);
});

test('checkFolderRow scans without importing, and carries the path as a query', async () => {
	routeFetch({ '/api/v1/setup/detect/folder': folderScan() });
	wizard.folderRows = [folderRow('  /Users/dj/Music  ')];

	await wizard.checkFolderRow(wizard.folderRows[0].id);

	const url = new URL(requests[0].url);
	assert.equal(url.pathname, '/api/v1/setup/detect/folder');
	assert.equal(url.searchParams.get('path'), '/Users/dj/Music');
	assert.equal(requests[0].method, 'GET');
	assert.equal(wizard.folderRows[0].scan.audio_files, 12);
	assert.equal(wizard.folderRows[0].path, '/Users/dj/Music');
});

test('checkFolderRow refuses an empty path without issuing a request', async () => {
	routeFetch({});
	wizard.folderRows = [folderRow('   ')];
	await wizard.checkFolderRow(wizard.folderRows[0].id);
	assert.equal(requests.length, 0);
	assert.match(wizard.error, /Type a folder path/);
});

test('beginFolderImport refuses before requesting when nothing was scanned', async () => {
	routeFetch({});
	await wizard.beginFolderImport();
	assert.equal(requests.length, 0);
	assert.equal(wizard.step, 'welcome');
});

test('beginFolderImport posts the folder and advances', async () => {
	let body;
	routeFetch({
		'/api/v1/setup/detect/folder': folderScan(),
		'/api/v1/setup/import/folder': async (request) => {
			body = await request.clone().json();
			return jsonResponse(job({ kind: 'setup.import-rekordbox' }), 202);
		}
	});

	wizard.folderRows = [folderRow('/Users/dj/Music')];
	await wizard.checkFolderRow(wizard.folderRows[0].id);
	await wizard.beginFolderImport();

	assert.deepEqual(body, { folders: ['/Users/dj/Music'] });
	assert.equal(wizard.step, 'progress');
});

test('beginFolderImport posts every validated folder row', async () => {
	let body;
	routeFetch({
		'/api/v1/setup/detect/folder': (request, url) => {
			const path = url.searchParams.get('path');
			return jsonResponse(
				folderScan({
					path,
					sample: [`${path}/a.wav`]
				})
			);
		},
		'/api/v1/setup/import/folder': async (request) => {
			body = await request.clone().json();
			return jsonResponse(job({ kind: 'setup.import-rekordbox' }), 202);
		}
	});

	wizard.folderRows = [
		folderRow('/Users/dj/Music', null, 'row-1'),
		folderRow('/Users/dj/Other', null, 'row-2')
	];
	await wizard.checkFolderRow('row-1');
	await wizard.checkFolderRow('row-2');
	await wizard.beginFolderImport();

	assert.deepEqual(body, { folders: ['/Users/dj/Music', '/Users/dj/Other'] });
});

test('a 403 on the folder import keeps the grant instructions verbatim', async () => {
	routeFetch({
		'/api/v1/setup/detect/folder': folderScan(),
		'/api/v1/setup/import/folder': () =>
			jsonResponse(
				{
					detail: {
						code: 'music_folder_access_denied',
						message: 'macOS refused to list /Users/dj/Music. Open System Settings'
					}
				},
				403
			)
	});

	wizard.folderRows = [folderRow('/Users/dj/Music')];
	await wizard.checkFolderRow(wizard.folderRows[0].id);
	await wizard.beginFolderImport();

	assert.match(wizard.error, /Open System Settings/);
	assert.match(wizard.error, /~\/Music/);
	assert.doesNotMatch(wizard.error, /\/Users\//);
	assert.equal(wizard.step, 'welcome');
});

// ------------------------------------------------------------------ dismissal

test('skip persists engine-side rather than in this tab', async () => {
	let body;
	routeFetch({
		'/api/v1/setup/dismiss': async (request) => {
			body = await request.clone().json();
			return jsonResponse(status({ dismissed: true, should_show_wizard: false }));
		}
	});

	await wizard.skip();

	assert.deepEqual(body, { dismissed: true });
	assert.equal(wizard.status.should_show_wizard, false);
});

test('STANDALONE-08: declining clears the source, so every reopen door is neutral', async () => {
	// Codex P2 on #3561: the incomplete chip's "Run setup" raises the overlay
	// WITHOUT setupWizard.reopen(), so the reset has to happen on the way out.
	routeFetch({ '/api/v1/setup/dismiss': status({ dismissed: true }) });
	wizard.useSource('rekordbox');

	await wizard.skip();

	assert.equal(wizard.error, null);
	assert.equal(wizard.status.dismissed, true);
	assert.equal(wizard.source, null, 'a declined import must not survive as a selection');
});

// ------------------------------------------------------------- finishing

function preflightCheck(id, checkStatus, detail) {
	return { id, label: id, status: checkStatus, detail, remediation: null };
}

/** GET /api/v1/preflight with the two rows the empty-library gate reads. */
function preflightReading(libraryStatus, libraryDetail) {
	return {
		status: 'pass',
		checks: [
			preflightCheck('engine-alive', 'pass', 'the endpoint answered'),
			preflightCheck('library-attached', libraryStatus, libraryDetail)
		]
	};
}

/** Answer successive preflight GETs from `readings`, one each, in order. */
function preflightSequence(readings) {
	let index = 0;
	return () => {
		const reading = readings[Math.min(index, readings.length - 1)];
		index += 1;
		return typeof reading === 'number'
			? jsonResponse({ detail: 'preflight exploded' }, reading)
			: jsonResponse(reading);
	};
}

function requestLine(request) {
	return `${request.method} ${new URL(request.url).pathname}`;
}

test('finish re-reads preflight, so the boot reading cannot re-raise setup after an import', async () => {
	// demon-llama, Thu 1 Oct 2026: 1274 tracks imported, eleven dismissals
	// all 200, and the overlay never left, because the root layout's gate was
	// still judging the empty library it saw at boot.
	routeFetch({
		'/api/v1/preflight': preflightSequence([
			preflightReading('fail', '0 tracks in the library'),
			preflightReading('pass', '1274 tracks')
		]),
		'/api/v1/setup/dismiss': status({ dismissed: true, library_empty: false, tracks: 1274 })
	});
	await mod.checkPreflight();
	assert.equal(
		mod.needsSetupForEmptyLibrary(mod.preflightGate.checks, false),
		true,
		'precondition: the boot reading asks for setup'
	);
	requests = [];

	const closable = await wizard.finish();

	assert.equal(wizard.error, null);
	assert.equal(closable, true);
	assert.deepEqual(requests.map(requestLine), [
		'POST /api/v1/setup/dismiss',
		'GET /api/v1/preflight'
	]);
	assert.equal(
		mod.needsSetupForEmptyLibrary(mod.preflightGate.checks, false),
		false,
		'after finish the gate must judge the library as it is now'
	);
	assert.equal(wizard.busy, false);
});

test('finish refuses with the engine detail when preflight still says no library', async () => {
	routeFetch({
		'/api/v1/preflight': preflightSequence([
			preflightReading('fail', 'the setup record at /data/setup.json could not be read')
		]),
		'/api/v1/setup/dismiss': status({ dismissed: true })
	});

	const closable = await wizard.finish();

	assert.equal(closable, false);
	assert.match(wizard.error, /library still looks empty/);
	assert.doesNotMatch(wizard.error, /\/data\/|\/api\//);
	assert.match(wizard.errorDiagnostic, /setup record at \/data\/setup\.json could not be read/);
	assert.equal(wizard.busy, false);
});

test('finish refuses, loudly, when preflight cannot be re-read', async () => {
	routeFetch({
		'/api/v1/preflight': preflightSequence([500]),
		'/api/v1/setup/dismiss': status({ dismissed: true })
	});

	const closable = await wizard.finish();

	assert.equal(closable, false);
	assert.match(wizard.error, /could not confirm the library is ready/);
	assert.match(wizard.errorDiagnostic, /startup checks could not be re-read/);
	assert.equal(wizard.busy, false);
});

test('finish asks preflight nothing when the dismissal itself was refused', async () => {
	routeFetch({
		'/api/v1/preflight': preflightSequence([preflightReading('pass', '1274 tracks')]),
		'/api/v1/setup/dismiss': () => jsonResponse({ detail: 'disk full' }, 500)
	});

	const closable = await wizard.finish();

	assert.equal(closable, false);
	assert.notEqual(wizard.error, null);
	assert.deepEqual(requests.map(requestLine), ['POST /api/v1/setup/dismiss']);
});

test('reopen re-arms the wizard and returns it to the first step', async () => {
	routeFetch({ '/api/v1/setup/dismiss': status({ dismissed: false }) });
	wizard.useSource('rekordbox');
	wizard.goTo('done');

	await wizard.reopen();

	assert.equal(wizard.step, 'welcome');
	assert.equal(wizard.source, null, 'reopen must clear the prior source choice');
	assert.equal(wizard.status.dismissed, false);
});

// ------------------------------------------------------- honest denominators

test('accessCaveat is null when nothing was blocked', () => {
	assert.equal(mod.accessCaveat(permissions()), null);
	assert.equal(mod.accessCaveat(null), null);
});

test('accessCaveat names blocked folders without raw paths in operator copy', () => {
	const caveat = mod.accessCaveat(
		permissions({ all_readable: false, denied: ['/Users/dj/Music'] })
	);
	assert.doesNotMatch(caveat, /\/Users\//);
	assert.match(caveat, /only what was accessible/);
	assert.equal(mod.agentAccessDetail(permissions({ all_readable: false, denied: ['/Users/dj/Music'] })), '/Users/dj/Music');
});

test('folderVerdict never quotes a file count for a denied folder', () => {
	const verdict = mod.folderVerdict(
		folderScan({ denied: true, readable: false, audio_files: 0 })
	);
	assert.doesNotMatch(verdict, /0 audio file/);
	assert.match(verdict, /System Settings/);
});

test('folderVerdict keeps the engine\'s plain permission reason, but never one carrying internals', () => {
	const said = mod.folderVerdict(
		folderScan({ denied: true, readable: false, audio_files: 0, detail: 'macOS refused the listing (Permission denied)' })
	);
	assert.match(said, /^macOS refused the listing \(Permission denied\)\. /);
	const dirty = mod.folderVerdict(
		folderScan({ denied: true, readable: false, audio_files: 0, detail: "PermissionError: [Errno 13] '/Users/dj/Music/Locked'" })
	);
	assert.doesNotMatch(dirty, /\/Users\/|Errno/);
	assert.match(dirty, /^This folder cannot be read yet\. /);
});

test('folderVerdict distinguishes empty from missing from unreadable', () => {
	assert.match(mod.folderVerdict(folderScan({ audio_files: 0 })), /holds no audio files/);
	assert.match(
		mod.folderVerdict(folderScan({ exists: false, readable: false })),
		/Nothing was found at/
	);
	assert.match(
		mod.folderVerdict(
			folderScan({ readable: false, detail: 'could not be listed: I/O error' })
		),
		/could not be read/
	);
	assert.doesNotMatch(
		mod.folderVerdict(
			folderScan({ readable: false, detail: 'could not be listed: I/O error' })
		),
		/I\/O error/
	);
});

test('folderVerdict reports skipped iCloud placeholders alongside the count', () => {
	const verdict = mod.folderVerdict(folderScan({ icloud_placeholders: 4 }));
	assert.match(verdict, /12 audio files/);
	assert.match(verdict, /iCloud-only files were skipped/);
});

test('folderIsImportable needs a readable folder with something in it', () => {
	assert.equal(mod.folderIsImportable(null), false);
	assert.equal(mod.folderIsImportable(folderScan({ audio_files: 0 })), false);
	assert.equal(mod.folderIsImportable(folderScan({ readable: false })), false);
	assert.equal(mod.folderIsImportable(folderScan()), true);
});

test('normalizeSetupFolderPath strips a trailing slash but preserves root', () => {
	assert.equal(mod.normalizeSetupFolderPath('/Users/dj/Music/'), '/Users/dj/Music');
	assert.equal(mod.normalizeSetupFolderPath('/'), '/');
});

// ------------------------------------------------------------- capability gate

test('a legacy daemon issues NO setup request and says why', async () => {
	const flavor = mod.capabilities.flavor;
	mod.capabilities.flavor = 'legacy';
	routeFetch({});
	try {
		await wizard.load();
		await wizard.redetect();
		wizard.folderRows = [folderRow('/Users/dj/Music')];
		await wizard.checkFolderRow(wizard.folderRows[0].id);
		await wizard.beginImport();
		await wizard.beginFolderImport();
		await wizard.skip();
		assert.equal(requests.length, 0, 'a legacy boot must not fire a setup request');
		assert.match(mod.setupRefusal(), /not offered by this daemon/);
	} finally {
		mod.capabilities.flavor = flavor;
	}
});

test('an unidentified daemon is inert too, and never treated as either one', () => {
	const flavor = mod.capabilities.flavor;
	mod.capabilities.flavor = 'unknown';
	try {
		assert.match(mod.setupRefusal(), /not identified yet/);
	} finally {
		mod.capabilities.flavor = flavor;
	}
});

test('the refusal wording is NOT the PARITY-TODO stub string', () => {
	// These features are built; they are simply not served by a legacy boot.
	// Reusing the "not implemented" wording would merge two different facts.
	const flavor = mod.capabilities.flavor;
	mod.capabilities.flavor = 'legacy';
	try {
		assert.doesNotMatch(mod.setupRefusal(), /PARITY-TODO/);
	} finally {
		mod.capabilities.flavor = flavor;
	}
});

// --------------------------------------------------------------- stage labels

test('every stage the server can report has a human label', () => {
	for (const stage of status().stages) {
		assert.ok(mod.STAGE_LABELS[stage], `no label for rekordbox stage ${stage}`);
	}
	for (const stage of status().folder_stages) {
		assert.ok(mod.FOLDER_STAGE_LABELS[stage], `no label for folder stage ${stage}`);
	}
});

// ------------------------------------------------- status after the import
//
// The Done step reads `status.last_import`. `status` was loaded when the
// overlay opened, before any import, so after a successful import the Done
// step said "No import was recorded for this data directory" (demon-llama
// preview, Thu 1 Oct 2026). The wizard re-reads status once its own import
// job settles.

function rekordboxImportSummary() {
	return {
		kind: 'rekordbox',
		finished_at: '2026-10-01T06:00:00Z',
		tracks: 1200,
		playlists: 34,
		analyses_linked: 1100,
		analyses_expected: 1200,
		share_root: '/Users/dj/Library/Pioneer/rekordbox/share'
	};
}

async function openedBeforeImport() {
	routeFetch({ '/api/v1/setup/status': status() });
	await wizard.load();
	assert.equal(wizard.status.last_import, null, 'precondition: the overlay opened before any import');
	wizard.jobId = 'job-setup-1';
	requests = [];
	routeFetch({
		'/api/v1/setup/status': status({ library_empty: false, tracks: 1200, last_import: rekordboxImportSummary() })
	});
}

test('a settled import re-reads status so the Done step shows the real import', async () => {
	await openedBeforeImport();

	await wizard.refreshStatusAfterImport(job({ status: 'succeeded', progress: 1 }).id);

	assert.deepEqual(requests.map((request) => request.url), [`${API_BASE}/api/v1/setup/status`]);
	assert.equal(wizard.status.last_import.tracks, 1200);
	assert.equal(wizard.status.tracks, 1200);
	assert.equal(wizard.error, null);
});

// "A failed import re-reads too" and "a live import does not re-read yet" are
// the overlay's call now (it passes a job id only for a terminal row, any
// outcome); setup-overlay.test.mjs pins that guard. The store's own rules
// stay here: once per job id, never for someone else's job.

test('the re-read happens once per settled job, not on every job-row update', async () => {
	await openedBeforeImport();
	const settled = job({ status: 'succeeded', progress: 1 });

	// The overlay calls again on every update of the settled row.
	await wizard.refreshStatusAfterImport(settled.id);
	await wizard.refreshStatusAfterImport(settled.id);

	assert.equal(requests.length, 1);
});

test("someone else's job never re-reads this wizard's status", async () => {
	await openedBeforeImport();

	await wizard.refreshStatusAfterImport(job({ id: 'job-other', status: 'succeeded' }).id);

	assert.deepEqual(requests, []);
});

test('a failed re-read says so and keeps the status it had', async () => {
	await openedBeforeImport();
	routeFetch({
		'/api/v1/setup/status': () =>
			jsonResponse({ detail: { code: 'boom', message: 'state db is gone' } }, 500)
	});

	await wizard.refreshStatusAfterImport(job({ status: 'succeeded', progress: 1 }).id);

	assert.equal(wizard.error, 'state db is gone');
	assert.equal(wizard.status.last_import, null, 'previous status must survive a failure');
});
