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
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { engineHealth, jsonResponse } from './setup-fixtures.mjs';

const API_BASE = 'https://setup.example.test';

let mod;
let wizard;
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

test('next and previous clamp at both ends rather than falling off', () => {
	assert.equal(mod.previousStep('welcome'), 'welcome');
	assert.equal(mod.nextStep('done'), 'done');
	assert.equal(mod.nextStep('welcome'), 'detect');
	assert.equal(mod.previousStep('done'), 'stems');
});

test('detect refuses Next until detection has answered', () => {
	const ctx = { source: 'rekordbox', detection: null, folderRows: emptyFolderRows(), job: null };
	assert.match(mod.advanceRefusal('detect', ctx), /has not answered/);
});

test('a fatal blocker refuses Next and names itself', () => {
	const ctx = {
		source: 'rekordbox',
		detection: detection({ blockers: ['rekordbox_not_found'] }),
		folderRows: emptyFolderRows(),
		job: null
	};
	assert.match(mod.advanceRefusal('detect', ctx), /rekordbox_not_found/);
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
	assert.equal(
		mod.advanceRefusal('progress', {
			...ctx,
			job: job({ status: 'succeeded', progress: 1 })
		}),
		null
	);
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

test('a failed load records the server message and KEEPS what was on screen', async () => {
	routeFetch({ '/api/v1/setup/status': status() });
	await wizard.load();

	routeFetch({
		'/api/v1/setup/status': () =>
			jsonResponse({ detail: { code: 'boom', message: 'state db is gone' } }, 500)
	});
	await wizard.load();

	assert.equal(wizard.error, 'state db is gone');
	assert.equal(wizard.status.tracks, 0, 'previous status must survive a failure');
});

test('redetect re-asks the detect endpoint specifically', async () => {
	routeFetch({ '/api/v1/setup/detect/rekordbox': detection({ installed: false }) });

	await wizard.redetect();

	assert.equal(requests[0].url, `${API_BASE}/api/v1/setup/detect/rekordbox`);
	assert.equal(wizard.detection.installed, false);
});

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

test('beginImport advances to progress and records the job id', async () => {
	routeFetch({ '/api/v1/setup/import': () => jsonResponse(job(), 202) });

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
	wizard.goTo('confirm');

	await wizard.beginImport();

	assert.equal(wizard.step, 'confirm', 'must not show progress for a job that was refused');
	assert.equal(wizard.jobId, null);
	assert.equal(wizard.error, 'setup import job-9 is already running');
});

test('refreshDecrypt is sent as the flag the CLI calls --refresh-decrypt', async () => {
	let body;
	routeFetch({
		'/api/v1/setup/import': async (request) => {
			body = await request.clone().json();
			return jsonResponse(job(), 202);
		}
	});

	await wizard.beginImport({ refreshDecrypt: true });

	assert.deepEqual(body, { refresh_decrypt: true });
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
	assert.match(wizard.error, /type a folder path/);
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

test('reopen re-arms the wizard and returns it to the first step', async () => {
	routeFetch({ '/api/v1/setup/dismiss': status({ dismissed: false }) });
	wizard.goTo('done');

	await wizard.reopen();

	assert.equal(wizard.step, 'welcome');
	assert.equal(wizard.status.dismissed, false);
});

// ------------------------------------------------------- honest denominators

test('accessCaveat is null when nothing was blocked', () => {
	assert.equal(mod.accessCaveat(permissions()), null);
	assert.equal(mod.accessCaveat(null), null);
});

test('accessCaveat names the folders and says the count is partial', () => {
	const caveat = mod.accessCaveat(
		permissions({ all_readable: false, denied: ['/Users/dj/Music'] })
	);
	assert.match(caveat, /\/Users\/dj\/Music/);
	assert.match(caveat, /only what could be read/);
});

test('folderVerdict never quotes a file count for a denied folder', () => {
	// 0 from a denied folder is a count of nothing, not a count of the folder.
	const verdict = mod.folderVerdict(
		folderScan({ denied: true, readable: false, audio_files: 0 })
	);
	assert.doesNotMatch(verdict, /0 audio file/);
	assert.match(verdict, /System Settings/);
});

test('folderVerdict distinguishes empty from missing from unreadable', () => {
	assert.match(mod.folderVerdict(folderScan({ audio_files: 0 })), /holds no audio files/);
	assert.match(
		mod.folderVerdict(folderScan({ exists: false, readable: false })),
		/Nothing at/
	);
	assert.match(
		mod.folderVerdict(
			folderScan({ readable: false, detail: 'could not be listed: I/O error' })
		),
		/I\/O error/
	);
});

test('folderVerdict reports skipped iCloud placeholders alongside the count', () => {
	const verdict = mod.folderVerdict(folderScan({ icloud_placeholders: 4 }));
	assert.match(verdict, /12 audio files/);
	assert.match(verdict, /4 more are iCloud placeholders/);
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
