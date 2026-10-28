/**
 * USB export screen state (USBEXP-01, USBEXP-02, USBEXP-03): the pure,
 * no-network half.
 *
 * Only logic that needs no API answer lives here, and no fetch is stubbed.
 * Everything that needs an answer runs against a real engine instead:
 * tests/e2e/usb-export-screen.spec.ts drives the screen in Library mode
 * (the refused-discovery state and a real 503 eject on a host without
 * discovery; a stick's menu and Check plan on a Mac with a stick), and the
 * routes themselves are covered by (private path) and
 * (private path).
 *
 * Regression one-liners:
 *   - if a bad track id is dropped instead of named then broken
 *   - if the one-way import gate's 403 shows as a bare HTTP status then broken
 *   - if a blank (non-music) stick is not offered as an export target then broken
 *   - if Check plan sends a request with no stick picked or a bad id then broken
 *   - if the screen can be closed while a plan, write or eject is in flight then broken
 *   - if a second entry point resets or re-targets the screen under a running operation then broken
 *   - if the close paths stay refused after the operation settles then broken
 *   - if a route change can unmount the screen while a plan, write or eject is in flight then broken
 *   - if a same-route navigation or one after the operation settled is refused then broken
 *   - if the desktop shell's quit (window close, Cmd-Q) is not refused while an operation runs then broken
 *   - if a write that failed with no answer leaves its plan writable again then broken
 *   - if a write the daemon refused (4xx) throws away a plan that is still good then broken
 *
 * The in-flight tests hold a REAL request open: planUsbExport really calls
 * fetch on the unroutable API_BASE, and nothing is replaced. The assertions
 * run while that request is pending, then again after it settles on its own
 * (with a transport problem, which is the real answer from that address).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://usb-export.example.test';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/usb-export.svelte.ts', { viteApiBase: API_BASE });
});

beforeEach(() => {
	Object.assign(mod.usbExport, {
		open: false,
		step: 'edit',
		targetPath: '/Volumes/GIG-A',
		targetId: 'vol:A1B2C3D4-0000-0000-0000-000000000001',
		templatePath: '/tmp/template/exportLibrary.db',
		playlistName: 'Warmup',
		trackIdsText: '1, 2',
		plan: null,
		readback: null,
		problem: null,
		ejecting: null,
		ejectNotice: null,
		ejectWatch: null,
		ejectProblem: null,
		busy: null,
		busyNotice: null
	});
});

test('parseTrackIds reads commas and spaces, and names a bad entry instead of dropping it', () => {
	assert.deepEqual(mod.parseTrackIds(' 1, 2 3,,4 '), [1, 2, 3, 4]);
	assert.deepEqual(mod.parseTrackIds(''), []);
	const bad = mod.parseTrackIds('1, two, 3');
	assert.equal(bad.code, 'track_ids_invalid');
	assert.match(bad.message, /"two"/);
	assert.equal(mod.parseTrackIds('0').code, 'track_ids_invalid');
});

test('parseTrackIds refuses an id Number() would round, and keeps the largest safe one (Sol P2 4287654324)', () => {
	const big = mod.parseTrackIds('9007199254740993');
	assert.equal(big.code, 'track_ids_invalid');
	assert.match(big.message, /"9007199254740993"/);
	assert.deepEqual(mod.parseTrackIds('9007199254740991'), [Number.MAX_SAFE_INTEGER]);
});

test('exportTargets lists every present real USB stick, music verdict or not', () => {
	const vols = [
		{ id: 'a', name: 'GIG-A', mount_path: '/Volumes/GIG-A', present: true, role: 'usb_stick', protocol: 'USB', is_music: true, first_seen: 0, last_seen: 0 },
		{ id: 'b', name: 'BLANK', mount_path: '/Volumes/BLANK', present: true, role: 'usb_stick', protocol: 'USB', is_music: false, first_seen: 0, last_seen: 0 },
		// Removable but not on USB: export and eject would refuse it (Sol P1 4350543033).
		{ id: 'f', name: 'SDCARD', mount_path: '/Volumes/SDCARD', present: true, role: 'usb_stick', protocol: 'Secure Digital', first_seen: 0, last_seen: 0 },
		{ id: 'g', name: 'NOBUS', mount_path: '/Volumes/NOBUS', present: true, role: 'usb_stick', protocol: null, first_seen: 0, last_seen: 0 },
		{ id: 'c', name: 'OLD', mount_path: '/Volumes/OLD', present: false, role: 'usb_stick', protocol: 'USB', first_seen: 0, last_seen: 0 },
		{ id: 'd', name: 'Backup', mount_path: '/Volumes/Backup', present: true, role: 'mounted_drive', first_seen: 0, last_seen: 0 },
		{ id: 'e', name: 'SIM', mount_path: '/Volumes/SIM', present: true, role: 'usb_stick', protocol: 'USB', simulated: true, first_seen: 0, last_seen: 0 }
	];
	assert.deepEqual(
		mod.exportTargets(vols, { scannedAt: 1, discovery: 'ok' }).map((v) => v.name),
		['GIG-A', 'BLANK']
	);
	// if restored rows listed before this session's first scan, Write and Eject
	// would act on last session's sticks, maybe another device at that path -- broken.
	assert.deepEqual(mod.exportTargets(vols, { scannedAt: null, discovery: 'ok' }), []);
	// if the last good scan stayed listed once discovery failed, the same -- broken.
	for (const discovery of ['unavailable', 'unreachable', 'unknown']) {
		assert.deepEqual(mod.exportTargets(vols, { scannedAt: 1, discovery }), [], discovery);
	}
});

test('describeUsbProblem reads both daemon error envelopes', () => {
	assert.deepEqual(
		mod.describeUsbProblem({ name: 'ApiError', status: 409, message: 'x', body: { detail: { code: 'target_not_empty', message: 'refusing target' } } }),
		{ code: 'target_not_empty', message: 'refusing target' }
	);
	// The one-way import gate answers {error, message}, not {detail}.
	assert.deepEqual(
		mod.describeUsbProblem({ name: 'ApiError', status: 403, message: 'Forbidden', body: { error: 'rekordbox_writeback_disabled', message: 'sync to rekordbox is disabled' } }),
		{ code: 'rekordbox_writeback_disabled', message: 'sync to rekordbox is disabled' }
	);
	// Discovery refusals carry `reason`, not `message`.
	assert.deepEqual(
		mod.describeUsbProblem({ name: 'ApiError', status: 503, message: 'x', body: { detail: { code: 'usb_eject_unavailable', reason: 'unsupported_platform:linux' } } }),
		{ code: 'usb_eject_unavailable', message: 'unsupported_platform:linux' }
	);
	assert.equal(mod.describeUsbProblem(new TypeError('fetch failed')).code, 'daemon_unreachable');
});

test('describeUsbProblem refuses an envelope that names a code but carries no sentence', () => {
	// if the code stood in for the missing sentence, contract drift would read as the daemon's own words -- broken.
	for (const body of [
		{ detail: { code: 'eject_failed' } },
		{ detail: { code: 'eject_failed', message: '' } },
		{ detail: { code: 'eject_failed', reason: '' } },
		{ error: 'rekordbox_writeback_disabled' },
		{ error: 'rekordbox_writeback_disabled', message: '' },
		// Whitespace is no sentence either (Sol P1 4351562923).
		{ detail: { code: 'eject_failed', message: '  ' } },
		{ detail: { code: 'eject_failed', message: ' \n', reason: '\t' } },
		{ error: 'rekordbox_writeback_disabled', message: ' ' }
	]) {
		const problem = mod.describeUsbProblem({ name: 'ApiError', status: 409, message: 'x', body });
		assert.equal(problem.code, 'daemon_envelope_malformed', JSON.stringify(body));
		assert.match(problem.message, /answered (eject_failed|rekordbox_writeback_disabled) without the sentence/);
	}
});

test('describeUsbProblem calls a FastAPI field-validation 422 malformed, never a made-up code (Sol P1 4349117026)', () => {
	const exc = { name: 'ApiError', status: 422, message: 'Unprocessable Entity', body: { detail: [{ loc: ['body', 'volume_id'], msg: 'Field required', type: 'missing' }] } };
	assert.deepEqual(mod.describeUsbProblem(exc), {
		code: 'daemon_envelope_malformed',
		message: 'the daemon answered HTTP 422 without an error envelope (body: object with detail)'
	});
	// It is refused before the route runs, so a write it answers still keeps its plan.
	assert.equal(mod.applyOutcomeUnknown(exc), false);
	// Control: the route's own 422 envelope is still the daemon's words.
	assert.deepEqual(
		mod.describeUsbProblem({ name: 'ApiError', status: 422, message: 'x', body: { detail: { code: 'plan_invalid', message: 'the plan does not parse' } } }),
		{ code: 'plan_invalid', message: 'the plan does not parse' }
	);
});

test('describeUsbProblem refuses an HTTP answer that carries neither envelope', () => {
	// if the status and client message stood in for the daemon's code and sentence, contract drift would read as a refusal -- broken.
	for (const [body, shape] of [
		[{}, 'empty object'],
		[{ detail: 'Not Found' }, 'object with detail'],
		[null, 'none'],
		['Internal Server Error', 'string'],
		// A blank code names nothing, so it is no envelope (Sol P1 4351562923).
		[{ detail: { code: '', message: 'disk full' } }, 'object with detail'],
		[{ detail: { code: '  ', message: 'disk full' } }, 'object with detail'],
		[{ error: ' ', message: 'sync is off' }, 'object with error, message']
	]) {
		const problem = mod.describeUsbProblem({ name: 'ApiError', status: 500, message: 'Internal Server Error', body });
		assert.equal(problem.code, 'daemon_envelope_malformed', JSON.stringify(body));
		assert.equal(problem.message, `the daemon answered HTTP 500 without an error envelope (body: ${shape})`);
	}
	// Control: a well-formed envelope on the same status still reads as the daemon's own words.
	assert.deepEqual(
		mod.describeUsbProblem({ name: 'ApiError', status: 500, message: 'x', body: { detail: { code: 'apply_failed', message: 'disk full' } } }),
		{ code: 'apply_failed', message: 'disk full' }
	);	// Control: a sentence with words is the daemon's own, padding and all.
	assert.deepEqual(
		mod.describeUsbProblem({ name: 'ApiError', status: 500, message: 'x', body: { detail: { code: 'apply_failed', message: ' disk full\n' } } }),
		{ code: 'apply_failed', message: ' disk full\n' }
	);
});

test('planUsbExport refuses before any request when no stick is picked or an id is bad', async () => {
	// No fetch is replaced: a request that went out to the unroutable API_BASE
	// would end in a transport or HTTP problem, never in these local codes.
	mod.usbExport.targetPath = null;
	await mod.planUsbExport();
	assert.equal(mod.usbExport.problem.code, 'target_missing');
	assert.equal(mod.usbExport.step, 'edit');

	mod.usbExport.targetPath = '/Volumes/GIG-A';
	mod.usbExport.trackIdsText = '1, x';
	await mod.planUsbExport();
	assert.equal(mod.usbExport.problem.code, 'track_ids_invalid');
	assert.equal(mod.usbExport.step, 'edit');
	assert.equal(mod.usbExport.plan, null);
});

test('both entry points open the screen: the bottom bar button and the USBs view row (USBEXP-01)', () => {
	const panel = readFileSync('src/lib/components/rb/BrowserPanel.svelte', 'utf8');
	assert.match(panel, /data-testid="bottom-bar-export-eject"[\s\S]{0,300}onclick=\{openUsbExport\}/);
	assert.doesNotMatch(panel, /plannedTitle\('export-eject'\)/, 'the bottom bar button is no longer a planned stub');
	assert.match(panel, /<UsbExportScreenLoader \/>/);
	const list = readFileSync('src/lib/components/rb/browser/UsbSourceList.svelte', 'utf8');
	assert.match(list, /data-testid="usb-source-export"[\s\S]{0,200}onclick=\{openUsbExport\}/);
});

async function settle(promise) {
	// A real request to an unroutable host ends on its own; bound the wait so a
	// host that black-holes it fails loud instead of hanging the suite.
	let timer;
	const timeout = new Promise((_, reject) => {
		timer = setTimeout(() => reject(new Error('the pending request never settled')), 60_000);
	});
	try {
		await Promise.race([promise, timeout]);
	} finally {
		clearTimeout(timer);
	}
}

test('while a plan is in flight the screen cannot be closed; once it settles it can', async () => {
	mod.usbExport.open = true;
	const pending = mod.planUsbExport();
	assert.equal(mod.usbExport.busy?.kind, 'plan', 'the plan claimed the operation slot');
	assert.equal(mod.usbExportBusy(), true);

	assert.equal(mod.closeUsbExport(), false, 'close (button, Escape, backdrop) is refused while busy');
	assert.equal(mod.usbExport.open, true, 'the screen stays visible with its progress');
	assert.match(mod.usbExport.busyNotice, /Checking the plan - wait for it to finish/);

	await settle(pending);
	assert.equal(mod.usbExport.busy, null, 'the slot is released when the request settles');
	assert.equal(mod.usbExport.busyNotice, null);
	assert.notEqual(mod.usbExport.problem, null, 'the real (failed) answer reached the screen');
	assert.equal(mod.usbExport.step, 'edit');
	assert.equal(mod.closeUsbExport(), true, 'close works again after the operation settles');
	assert.equal(mod.usbExport.open, false);
});

test('a route change is refused while busy; a same-route one and any after it settles are not', async () => {
	mod.usbExport.open = false;
	assert.equal(mod.refuseNavigationWhileUsbBusy('/', '/performance'), false, 'idle: nothing to protect');
	const pending = mod.planUsbExport();
	assert.equal(mod.usbExportBusy(), true);

	assert.equal(mod.refuseNavigationWhileUsbBusy('/', '/performance'), true, 'leaving the route would unmount the screen');
	assert.equal(mod.refuseNavigationWhileUsbBusy('/', null), true, 'an unknown destination is refused too');
	assert.equal(mod.usbExport.open, true, 'the screen is up to say why');
	assert.match(mod.usbExport.busyNotice, /Checking the plan - wait for it to finish/);
	assert.equal(mod.refuseNavigationWhileUsbBusy('/', '/'), false, 'same route keeps the screen mounted');
	assert.equal(mod.refuseNavigationWhileUsbBusy('/', '/', false), false, 'a client-side same-route move keeps it mounted');
	assert.equal(mod.refuseNavigationWhileUsbBusy('/', '/', true), true, 'a reload of the same route unloads the document');
	assert.equal(mod.refuseNavigationWhileUsbBusy('/', '/performance', true), true, 'an unload to another route is refused');

	await settle(pending);
	assert.equal(mod.refuseNavigationWhileUsbBusy('/', '/performance'), false, 'settled: navigation works again');
	assert.equal(mod.refuseNavigationWhileUsbBusy('/', '/', true), false, 'settled: a reload works again');
});

test('the mount point hands the unload flag to the veto, so a same-route reload is refused (Sol P1 4397722418)', () => {
	const loader = readFileSync('src/lib/components/rb/UsbExportScreenLoader.svelte', 'utf8');
	assert.match(
		loader,
		/refuseNavigationWhileUsbBusy\(\s*navigation\.from\?\.route\.id \?\? null,\s*navigation\.to\?\.route\.id \?\? null,\s*navigation\.willUnload\s*\)/
	);
});

test('a running operation holds the quit veto the store registers, and releases it once settled', async () => {
	// The store and the gate state in ONE bundle, so the store's own
	// registration at load is what the gate consults; nothing is registered here.
	const both = await loadTypeScriptModule('tests/unit/fixtures/usb-export-quit-gate-entry.ts', { viteApiBase: API_BASE });
	Object.assign(both.usbExport, { ...mod.usbExport }); // the same inputs the other tests start from
	assert.equal(both.quitVetoed(), false, 'idle: nothing holds the quit');
	const pending = both.planUsbExport();
	assert.equal(both.usbExportBusy(), true, 'the plan really started');
	assert.equal(both.quitVetoed(), true, 'a running plan holds the quit');
	assert.equal(both.usbExport.open, true, 'the screen is up to say why');
	assert.match(both.usbExport.busyNotice, /Checking the plan - wait for it to finish/);

	await settle(pending);
	assert.equal(both.quitVetoed(), false, 'settled: the quit goes ahead');
});

test('the shell quit path reaches the veto and the engine shutdown does not (source pin; real shell UNAVAILABLE here)', () => {
	// Driving window close or Cmd-Q needs the packaged desktop app on macOS
	// (apps/desktop/tests/quit-confirm.e2e.ts), which this host cannot run, so
	// the delivery is pinned link by link instead of faked (Codex P1 4301528198).
	const read = (rel) => readFileSync(new URL(rel, import.meta.url), 'utf8');
	const gate = read('../../src/lib/shell/quit-gate.ts');
	const handle = gate.slice(gate.indexOf('export function handleQuitRequest'));
	assert.match(
		handle,
		/if \(options\.force !== true && quitVetoed\(\)\) \{\s*closeQuitConfirm\(\);\s*return;\s*\}\s*const action = planQuitRequest\(/,
		'a user quit checks the veto before it can confirm or exit'
	);
	const install = gate.slice(gate.indexOf('export function installQuitGate'));
	assert.match(install, /const requestQuit = \(\): void => handleQuitRequest\(\{ source: 'shell' \}, resolved\);/);
	assert.match(install, /\.__OPENDJ_requestQuit = requestQuit;/);
	assert.match(install, /handleQuitRequest\(\{ force: true, source: 'engine' \}, resolved\);/);
	// Both shells hand window close and Cmd-Q to that hook.
	const electron = read('../../../../desktop/electron/src/main.ts');
	assert.match(electron, /executeJavaScript\('globalThis\.__OPENDJ_requestQuit\?\.\(\)'\)/);
	const tauri = read('../../../../desktop/src-tauri/src/main.rs');
	assert.match(tauri, /window\.eval\("globalThis\.__OPENDJ_requestQuit\?\.\(\)"\)/);
});

test('a second entry point is refused while busy and leaves the running operation its state', async () => {
	mod.usbExport.open = true;
	const pending = mod.planUsbExport();
	const owner = mod.usbExport.busy;
	assert.notEqual(owner, null);

	assert.equal(mod.openUsbExportForPlaylist('Peak Time'), false, 'another playlist cannot re-target the screen');
	assert.equal(mod.usbExport.playlistName, 'Warmup');
	mod.resetUsbExportPlan();
	assert.equal(mod.usbExport.step, 'planning', 'an input edit cannot reset the running plan');
	await mod.ejectUsbStick('/Volumes/GIG-A', 'vol:A1B2-C3');
	assert.equal(mod.usbExport.ejecting, null, 'the stick menu cannot start an eject alongside it');
	await mod.planUsbExport();
	assert.equal(mod.usbExport.busy?.token, owner.token, 'refused entries never take or release the slot');
	assert.match(mod.usbExport.busyNotice, /wait for it to finish/);

	await settle(pending);
	assert.equal(mod.usbExport.busy, null);
	assert.equal(mod.usbExport.playlistName, 'Warmup', 'the first operation settled on its own inputs');
	// Control: the same entry point works once nothing is running.
	assert.equal(mod.openUsbExportForPlaylist('Peak Time'), true);
	assert.equal(mod.usbExport.playlistName, 'Peak Time');
	assert.equal(mod.usbExport.plan, null);
});

test('the playlist menu starts a new export even when the playlist name matches the last one (Codex 4286356460)', () => {
	// Two playlists can share a name; a plan checked for one must not open
	// ready to write for the other.
	mod.usbExport.plan = { plan_id: 'p-1', volume_label: 'GIG-A' };
	mod.usbExport.step = 'planned';
	assert.equal(mod.openUsbExportForPlaylist('Warmup'), true);
	assert.equal(mod.usbExport.plan, null, 'if the old plan survives a same-name playlist, Write stays armed - broken');
	assert.equal(mod.usbExport.step, 'edit');
	assert.equal(mod.usbExport.trackIdsText, '');
	assert.equal(mod.usbExport.playlistName, 'Warmup');
	assert.equal(mod.usbExport.open, true);
});

test('every close path on the screen goes through the busy-aware closeUsbExport, and the x is disabled while busy', () => {
	// No real way exists on a stick-less host to hold a device request open in a
	// browser (Check plan and Eject need a stick), so the browser half of this is
	// pinned at the source; the behavior is the store tests above.
	const screen = readFileSync('src/lib/components/rb/UsbExportScreen.svelte', 'utf8');
	assert.match(screen, /const busy = \$derived\(usbExport\.busy !== null\);/);
	// A modal <dialog> (top layer, inert page, focus kept inside; Codex P1
	// 4286889359): Escape arrives as cancel, the backdrop as a click on the
	// dialog element itself, and both go to the store.
	assert.match(screen, /<dialog[\s\S]{0,200}oncancel=\{onCancel\}[\s\S]{0,60}onclick=\{onDialogClick\}/);
	assert.match(screen, /if \(!el\.open\) el\.showModal\(\);/);
	assert.match(screen, /function onCancel\(e: Event\): void \{\s*(\/\/[^\n]*\s*)*e\.preventDefault\(\);\s*closeUsbExport\(\);/);
	assert.match(screen, /if \(e\.target === dialogEl\) closeUsbExport\(\);/);
	assert.doesNotMatch(screen, /role="dialog"/, 'no hand-rolled dialog layered by z-index');
	assert.match(screen, /onclick=\{closeUsbExport\}\s*disabled=\{busy\}\s*data-testid="usb-export-close"/);
	assert.match(screen, /data-testid="usb-export-busy"/);
	assert.doesNotMatch(screen, /usbExport\.open = false/, 'no close path bypasses the store');
});

test('applyOutcomeUnknown: no answer or a 5xx is unknown, a 4xx refusal is not', () => {
	assert.equal(mod.applyOutcomeUnknown(new TypeError('fetch failed')), true, 'transport failure');
	assert.equal(mod.applyOutcomeUnknown({ name: 'ApiError', status: 500, message: 'x' }), true);
	assert.equal(mod.applyOutcomeUnknown({ name: 'ApiError', status: 502, message: 'x' }), true);
	assert.equal(mod.applyOutcomeUnknown(null), true);
	// Control, the opposite direction: a well-formed refusal before writing keeps the plan.
	const refused = (status, code) => ({ name: 'ApiError', status, message: 'x', body: { detail: { code, message: 'refused' } } });
	assert.equal(mod.applyOutcomeUnknown(refused(409, 'target_not_empty')), false);
	assert.equal(mod.applyOutcomeUnknown(refused(422, 'plan_invalid')), false);
	assert.equal(mod.applyOutcomeUnknown({ name: 'ApiError', status: 422, message: 'x', body: { detail: [] } }), false, 'field validation');
});

test('applyOutcomeUnknown: a 4xx whose envelope is malformed retires the plan', () => {
	// if a post-write code that lost its sentence read as a pre-write refusal, Write would be offered again on a touched stick -- broken.
	for (const body of [
		{ detail: { code: 'rollback_refused' } },
		{ detail: { code: 'rollback_refused', message: '' } },
		{},
		null
	]) {
		assert.equal(mod.applyOutcomeUnknown({ name: 'ApiError', status: 409, message: 'x', body }), true, JSON.stringify(body));
	}
});

test('applyOutcomeUnknown: a 409 the daemon raised after it wrote the stick still retires the plan', () => {
	// export_workflow raises rollback_refused after promotion: the stick was
	// written and cleanup was refused, so retrying the same plan is wrong.
	const spent = { name: 'ApiError', status: 409, message: 'x', body: { detail: { code: 'rollback_refused', message: 'cleanup refused' } } };
	assert.equal(mod.applyOutcomeUnknown(spent), true);
	// Control: a 409 refused before writing (another code) keeps the plan.
	const before = { name: 'ApiError', status: 409, message: 'x', body: { detail: { code: 'plan_stale', message: 'stick changed' } } };
	assert.equal(mod.applyOutcomeUnknown(before), false);
});

test('applyOutcomeUnknown: a failed promotion retires the plan, since its folders are already on the stick (Codex 4291212605)', () => {
	// _promote_new_file creates PIONEER/rekordbox before it copies, and a
	// failure removes only the .part file, so the same plan would next be
	// refused as target_not_empty, hiding the partial write.
	const workflow = readFileSync(fileURLToPath(new URL('../../../../../apps/sync/usb/pioneer/export_workflow.py', import.meta.url)), 'utf8');
	const promote = workflow.slice(workflow.indexOf('def _promote_new_file('), workflow.indexOf('def _rollback_promoted_output('));
	assert.match(promote, /target_output\.parent\.mkdir\(/, 'control: promotion creates the export folders first');
	// target_output_exists too: a late collision lands after the mkdir (Codex P2 4293492013).
	// target_changed: the mkdir itself found the folders another writer made (Codex P2 4300723944).
	for (const code of ['promotion_failed', 'promotion_hash_mismatch', 'target_output_exists', 'target_changed']) {
		assert.match(promote, new RegExp(`"${code}"`), `control: the daemon raises ${code} during promotion`);
		const failed = { name: 'ApiError', status: 409, message: 'x', body: { detail: { code, message: 'x' } } };
		assert.equal(mod.applyOutcomeUnknown(failed), true, `if ${code} keeps the plan then broken`);
	}
});

test('a write that fails with no answer retires its plan and says the stick may be touched', async () => {
	mod.usbExport.open = true;
	mod.usbExport.plan = { plan_id: 'p-1', volume_label: 'GIG-A' };
	mod.usbExport.step = 'planned';
	// A real request to the unroutable API_BASE: the apply gets no answer.
	await settle(mod.writeUsbExport());
	assert.equal(mod.usbExport.problem?.code, 'write_outcome_unknown');
	assert.match(mod.usbExport.problem.message, /may have started/);
	assert.equal(mod.usbExport.plan, null, 'the plan cannot be written again blind');
	assert.equal(mod.usbExport.step, 'edit');
});

test('a write warning outlives the stick leaving the scan; only the pick and plan go (Codex P2 4293176272)', async () => {
	mod.usbExport.open = true;
	mod.usbExport.plan = { plan_id: 'p-1', volume_label: 'GIG-A' };
	mod.usbExport.step = 'planned';
	// A real request to the unroutable API_BASE: the apply gets no answer.
	await settle(mod.writeUsbExport());
	assert.equal(mod.usbExport.problem?.code, 'write_outcome_unknown');
	mod.forgetVanishedTarget();
	assert.equal(mod.usbExport.targetPath, null);
	assert.equal(mod.usbExport.targetId, null);
	assert.equal(mod.usbExport.plan, null);
	assert.equal(mod.usbExport.problem?.code, 'write_outcome_unknown', 'the stick may have been changed: say so');
	assert.equal(mod.usbExport.step, 'edit');
});

test('a vanished stick still clears its plan and a planning refusal (the opposite direction)', () => {
	mod.usbExport.plan = { plan_id: 'p-1', volume_label: 'GIG-A' };
	mod.usbExport.step = 'planned';
	mod.usbExport.problem = { code: 'target_not_empty', message: 'The stick already has an export.' };
	mod.forgetVanishedTarget();
	assert.equal(mod.usbExport.plan, null);
	assert.equal(mod.usbExport.problem, null, 'a refusal about the gone stick is stale');
	assert.equal(mod.usbExport.step, 'edit');
	// A verified write keeps its readback and its done step.
	mod.usbExport.targetPath = '/Volumes/GIG-A';
	mod.usbExport.plan = { plan_id: 'p-2', volume_label: 'GIG-A' };
	mod.usbExport.readback = { verified: true };
	mod.usbExport.step = 'done';
	mod.forgetVanishedTarget();
	assert.deepEqual(mod.usbExport.readback, { verified: true });
	assert.equal(mod.usbExport.step, 'done');
	assert.equal(mod.usbExport.plan?.volume_label, 'GIG-A', 'the done line still names the stick it wrote (Codex P2 4293421286)');
	assert.equal(mod.usbExport.targetPath, null);
	// While an operation runs it owns these fields: nothing changes.
	mod.usbExport.targetPath = '/Volumes/GIG-A';
	mod.usbExport.busy = 'plan';
	mod.forgetVanishedTarget();
	assert.equal(mod.usbExport.targetPath, '/Volumes/GIG-A');
});

test('an idle USB screen closes when the quit confirm opens, so the confirm is not hidden under it (Codex P2 4293176283)', () => {
	mod.usbExport.open = true;
	mod.usbExport.plan = { plan_id: 'p-1', volume_label: 'GIG-A' };
	mod.closeForQuitConfirm(false);
	assert.equal(mod.usbExport.open, true, 'the confirm closing, or not open, leaves the screen alone');
	mod.closeForQuitConfirm(true);
	assert.equal(mod.usbExport.open, false);
	assert.deepEqual(mod.usbExport.plan, { plan_id: 'p-1', volume_label: 'GIG-A' }, 'the plan survives for the reopen');
	// Opposite direction: a running operation keeps its screen (the quit is vetoed then).
	mod.usbExport.open = true;
	mod.usbExport.busy = 'write';
	mod.closeForQuitConfirm(true);
	assert.equal(mod.usbExport.open, true);
	const store = readFileSync(new URL('../../src/lib/rb/usb-export.svelte.ts', import.meta.url), 'utf8');
	assert.match(store, /subscribeQuitConfirmOpen\(\(\) => closeForQuitConfirm\(isQuitConfirmOpen\(\)\)\);/);
});

test('a stick pulled out during another operation is cleared once that operation settles', () => {
	const screen = readFileSync(new URL('../../src/lib/components/rb/UsbExportScreen.svelte', import.meta.url), 'utf8');
	// resetUsbExportPlan is a no-op while busy, so clearing targetPath then would
	// strand the plan: the effect waits for !busy and re-runs when busy clears.
	assert.match(screen, /!busy &&\s*usbExport\.targetPath !== null &&\s*pickedStickGone\(usbTracker\.volumes, usbTracker, usbExport\.targetPath, usbExport\.targetId\)/);
	// Write and Check plan are inert while no healthy scan lists the pick
	// (targetBlocker is NO_TARGET whenever target is null).
	assert.match(screen, /const targetBlocker = \$derived\(target === null \? NO_TARGET : /);
	assert.match(screen, /data-testid="usb-export-plan"\s*disabled=\{busy \|\| targetBlocker !== null\}/);
	assert.match(screen, /data-testid="usb-export-write"\s*disabled=\{busy \|\| targetBlocker !== null\}/);
	assert.match(screen, /const busy = \$derived\(usbExport\.busy !== null\);/);
});

const UUID = 'A1B2C3D4-0000-0000-0000-000000000001';
const PICKED = { id: `vol:${UUID}`, name: 'GIG-A', mount_path: '/Volumes/GIG-A', present: true, role: 'usb_stick', protocol: 'USB', first_seen: 0, last_seen: 0 };

test('an eject notice ends when a listing requested after the eject shows a stick plugged in since (Codex P2 4301877384, 4301908865)', () => {
	const later = { scannedAt: 1, discovery: 'ok', listingSeq: 8 };
	const OTHER = { ...PICKED, id: 'vol:OTHER', name: 'GIG-B', mount_path: '/Volumes/GIG-B' };
	const SAME_LABEL = { ...PICKED, id: 'vol:REPLACEMENT' };
	const watch = { id: PICKED.id, others: [OTHER.id], sinceSeq: 7, afterSeq: 7 };
	// A same-label replacement at the ejected stick's path ends it.
	assert.equal(mod.nextEjectWatch(watch, [SAME_LABEL, OTHER], later), null);
	// A stick under a new name ends it too.
	assert.equal(mod.nextEjectWatch(watch, [{ ...OTHER, id: 'vol:NEW', mount_path: '/Volumes/NEW' }], later), null);
	// The same stick back in ends it, with no listing in between showing it gone.
	assert.equal(mod.nextEjectWatch(watch, [PICKED, OTHER], later), null);
	// Controls: sticks already listed at the eject keep it...
	assert.equal(mod.nextEjectWatch(watch, [OTHER], later), watch);
	// ...as does a listing requested before or at the eject, which may still show the stick...
	assert.equal(mod.nextEjectWatch(watch, [PICKED, OTHER], { ...later, listingSeq: 7 }), watch);
	// A listing requested while the eject ran (Sol P1 4349545524): a stick
	// plugged in since ends the notice, but the ejected stick alone does not,
	// since that listing may predate the eject.
	const during = { ...watch, sinceSeq: 5, afterSeq: 7 };
	assert.equal(mod.nextEjectWatch(during, [SAME_LABEL, OTHER], { ...later, listingSeq: 6 }), null);
	assert.equal(mod.nextEjectWatch(during, [PICKED, OTHER], { ...later, listingSeq: 6 }), during);
	assert.equal(mod.nextEjectWatch(during, [SAME_LABEL, OTHER], { ...later, listingSeq: 5 }), during, 'requested before the eject was sent');
	// ...and any outage.
	for (const discovery of ['unknown', 'unavailable', 'unreachable']) {
		assert.equal(mod.nextEjectWatch(watch, [SAME_LABEL], { ...later, discovery }), watch, discovery);
	}
	assert.equal(mod.nextEjectWatch(watch, [SAME_LABEL], { ...later, scannedAt: null }), watch, 'no scan yet');

	// The store follows a listing: the notice clears with the watch.
	mod.usbExport.ejectNotice = 'GIG-A ejected. You can pull it out.';
	mod.usbExport.ejectWatch = watch;
	mod.followEjectNotice([OTHER], later);
	assert.equal(mod.usbExport.ejectNotice, 'GIG-A ejected. You can pull it out.', 'control: the stick is gone, so the notice stays');
	mod.followEjectNotice([PICKED, OTHER], later);
	assert.equal(mod.usbExport.ejectNotice, null);
	assert.equal(mod.usbExport.ejectWatch, null);

	// Source pin: the screen follows every listing. What a successful eject
	// records is driven for real in the fixture-answer eject test below.
	const screen = readFileSync(new URL('../../src/lib/components/rb/UsbExportScreen.svelte', import.meta.url), 'utf8');
	assert.match(screen, /\$effect\(\(\) => \{\s*followEjectNotice\(usbTracker\.volumes, usbTracker\);\s*\}\);/);
});

test('pickedStickGone: only a healthy scan without the picked stick clears the pick', () => {
	const ok = { scannedAt: 1, discovery: 'ok' };
	// If a scan outage clears the pick, a just-published write result is erased -- broken.
	for (const discovery of ['unknown', 'unavailable', 'unreachable']) {
		assert.equal(mod.pickedStickGone([], { scannedAt: 1, discovery }, PICKED.mount_path, PICKED.id), false, discovery);
	}
	assert.equal(mod.pickedStickGone([], { scannedAt: null, discovery: 'ok' }, PICKED.mount_path, PICKED.id), false, 'no scan yet');
	// Control, the opposite direction: a healthy scan that lacks the stick does clear it.
	assert.equal(mod.pickedStickGone([], ok, PICKED.mount_path, PICKED.id), true);
	assert.equal(mod.pickedStickGone([{ ...PICKED, present: false }], ok, PICKED.mount_path, PICKED.id), true);
	// Another stick at the same path is not the picked one.
	const other = { ...PICKED, id: 'vol:FFFFFFFF-0000-0000-0000-000000000002' };
	assert.equal(mod.pickedStickGone([other], ok, PICKED.mount_path, PICKED.id), true);
	assert.equal(mod.pickedStickGone([PICKED], ok, PICKED.mount_path, PICKED.id), false, 'still there');
});

test('planMatchesTarget: a plan is kept only for the stick the user picked', () => {
	const root = PICKED.mount_path;
	const mine = { volume_uuid: UUID, target_root: root };
	assert.equal(mod.planMatchesTarget(mine, `vol:${UUID}`, root), true);
	// If stick B mounted at A's path while the plan ran, its plan is refused.
	assert.equal(mod.planMatchesTarget({ ...mine, volume_uuid: 'FFFFFFFF-0000-0000-0000-000000000002' }, `vol:${UUID}`, root), false);
	assert.equal(mod.planMatchesTarget(mine, `path:GIG-A`, root), false, 'a listing with no UUID proves nothing');
	assert.equal(mod.planMatchesTarget({ volume_uuid: '', target_root: root }, 'vol:', root), false);
	assert.equal(mod.planMatchesTarget(mine, null, root), false);
	// The picked UUID at another root is a plan for another mount (a clone of
	// the stick, or a stale answer): Write would run there (Codex 4352307431).
	assert.equal(mod.planMatchesTarget({ ...mine, target_root: '/Volumes/GIG-A 1' }, `vol:${UUID}`, root), false);
	assert.equal(mod.planMatchesTarget({ ...mine, target_root: '' }, `vol:${UUID}`, ''), false, 'an empty root names no mount');
});

test('stickActionBlocker: only a listing id carrying a volume UUID gets Check plan, Write and Eject (Sol P1 4398681354)', () => {
	assert.equal(mod.stickActionBlocker(`vol:${UUID}`), null);
	for (const id of ['path:GIG-A', 'vol:', 'a', '', null]) {
		assert.equal(mod.stickActionBlocker(id), mod.NO_VOLUME_UUID, String(id));
	}
	// The predicate agrees with the plan check it stands in front of: a UUID id
	// can keep its plan, a path: id never can.
	assert.equal(mod.planMatchesTarget({ volume_uuid: UUID, target_root: '/Volumes/GIG-A' }, `vol:${UUID}`, '/Volumes/GIG-A'), true);
	assert.equal(mod.planMatchesTarget({ volume_uuid: UUID, target_root: '/Volumes/GIG-A' }, 'path:GIG-A', '/Volumes/GIG-A'), false);
});

test('planUsbExport refuses a pick with no volume UUID before any request', async () => {
	mod.usbExport.targetId = 'path:GIG-A';
	await mod.planUsbExport();
	assert.equal(mod.usbExport.problem.code, 'target_unverifiable');
	assert.match(mod.usbExport.problem.message, /no volume UUID/);
	assert.equal(mod.usbExport.busy, null);
});

test('the screen lists a stick with no UUID but disables its Eject, Check plan and Write with the reason (source pin)', () => {
	const screen = readFileSync(new URL('../../src/lib/components/rb/UsbExportScreen.svelte', import.meta.url), 'utf8');
	assert.match(screen, /\{@const blocker = stickActionBlocker\(stick\.id\)\}/);
	assert.match(screen, /disabled=\{busy \|\| blocker !== null\}\s*title=\{blocker \?\? `Flush and unmount/);
	assert.match(screen, /const targetBlocker = \$derived\(target === null \? NO_TARGET : stickActionBlocker\(target\.id\)\)/);
	assert.equal((screen.match(/disabled=\{busy \|\| targetBlocker !== null\}\s*title=\{targetBlocker \?\? /g) ?? []).length, 2, 'Check plan and Write');
	// The list itself is not filtered by the UUID: the stick stays visible.
	assert.match(screen, /const sticks = \$derived\(exportTargets\(usbTracker\.volumes, usbTracker\)\)/);
});

test('planUsbExport refuses before any request when the pick has no listing id', async () => {
	mod.usbExport.targetId = null;
	await mod.planUsbExport();
	assert.equal(mod.usbExport.problem.code, 'target_missing');
	assert.equal(mod.usbExport.busy, null);
});

test('reopening the screen drops the last eject outcome; an open over a screen already up keeps it (Codex P2 4301471569)', () => {
	mod.usbExport.ejectNotice = 'GIG-A ejected. You can pull it out.';
	mod.usbExport.ejectWatch = { id: 'vol:A', others: [], sinceSeq: 0, afterSeq: 0 };
	mod.usbExport.ejectProblem = { code: 'eject_failed', message: 'busy' };
	mod.openUsbExport();
	assert.equal(mod.usbExport.ejectWatch, null);
	assert.equal(mod.usbExport.open, true);
	assert.equal(mod.usbExport.ejectNotice, null, 'a closed-then-opened screen must not tell the user to pull a stick');
	assert.equal(mod.usbExport.ejectProblem, null);

	// Control: the screen is already up (a second entry point, or the stick
	// menu opening it before its eject), so the outcome the user just saw stays.
	mod.usbExport.ejectNotice = 'GIG-A ejected. You can pull it out.';
	mod.openUsbExport();
	assert.equal(mod.usbExport.ejectNotice, 'GIG-A ejected. You can pull it out.');
});

/**
 * A daemon double for the eject: a local HTTP server that answers
 * POST /api/v1/usb/eject with a synthesized eject answer (fixtures/usb-eject-ok.json,
 * or with `busy` the route's 409 refusal, fixtures/usb-eject-busy.json; both
 * hand-written, not captured from a run, and checked against the route's real response models by
 * (private path)) and holds GET /api/v1/usb/volumes open until
 * released. The store under test, ejectUsbStick, runs its real code and makes
 * real requests (AGENTS.md "Test doubles and locked real fixtures": the daemon
 * is the dependency here, the store is the subject). The real route is covered
 * on a Mac with a stick by (private path) and
 * tests/e2e/usb-export-screen.spec.ts.
 */
test('a write the daemon refused after writing keeps its code and sentence and retires the plan (Sol P1 4349117035)', async () => {
	// The 409 envelope export_workflow raises when post-promotion cleanup is
	// refused. The fixture is hand-written, not captured from a run;
	// (private path) proves the apply route sends it.
	const refusal = readFileSync(new URL('./fixtures/usb-apply-rollback-refused.json', import.meta.url), 'utf8');
	const seen = [];
	const server = createServer((req, res) => {
		req.resume();
		req.on('end', () => {
			seen.push(`${req.method} ${req.url}`);
			const apply = req.method === 'POST' && req.url === '/api/v1/usb-export/apply';
			res.writeHead(apply ? 409 : 404, { 'content-type': 'application/json' });
			res.end(apply ? refusal : '{"detail":"not served by this double"}');
		});
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	try {
		const store = await loadTypeScriptModule('src/lib/rb/usb-export.svelte.ts', { viteApiBase: `http://127.0.0.1:${server.address().port}` });
		store.usbExport.open = true;
		store.usbExport.plan = { plan_id: 'p-1', volume_label: 'GIG-A' };
		store.usbExport.step = 'planned';
		await store.writeUsbExport();
		assert.deepEqual(seen, ['POST /api/v1/usb-export/apply']);
		assert.deepEqual(store.usbExport.problem, JSON.parse(refusal).detail, 'the daemon code, not write_outcome_unknown');
		assert.equal(store.usbExport.plan, null, 'the stick was written, so the plan cannot be written again');
		assert.equal(store.usbExport.step, 'edit');
	} finally {
		await new Promise((resolve) => server.close(resolve));
	}
});

/** A read-back answer in ReadbackModel's shape (every field the route sends). */
const READBACK = {
	plan_id: 'p-1',
	volume_uuid: UUID,
	output_relative_path: 'PIONEER/rekordbox/exportLibrary.db',
	output_sha256: '0'.repeat(64),
	playlists: [{ name: 'Friday', track_ids: [1, 2] }],
	track_updates: [],
	verified: true
};

/** An apply receipt in ReceiptModel's shape (every field the route sends). */
const RECEIPT = {
	plan_id: 'p-1',
	volume_uuid: UUID,
	output_relative_path: 'PIONEER/rekordbox/exportLibrary.db',
	output_sha256: '0'.repeat(64),
	playlist_ids: [1],
	verified: true
};

async function writeThenRead(
	applyVerified,
	readbackVerified,
	readbackAnswer = { ...READBACK, verified: readbackVerified },
	receiptAnswer = { ...RECEIPT, verified: applyVerified },
	seen = []
) {
	const answers = {
		'/api/v1/usb-export/apply': receiptAnswer,
		'/api/v1/usb-export/readback': readbackAnswer
	};
	const server = createServer((req, res) => {
		req.resume();
		req.on('end', () => {
			seen.push(req.url);
			const answer = req.method === 'POST' ? answers[req.url] : undefined;
			res.writeHead(answer ? 200 : 404, { 'content-type': 'application/json' });
			res.end(answer ? JSON.stringify(answer) : '{"detail":"not served by this double"}');
		});
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	try {
		const store = await loadTypeScriptModule('src/lib/rb/usb-export.svelte.ts', { viteApiBase: `http://127.0.0.1:${server.address().port}` });
		store.usbExport.open = true;
		store.usbExport.plan = { plan_id: 'p-1', volume_label: 'GIG-A', volume_uuid: UUID, output_relative_path: READBACK.output_relative_path };
		store.usbExport.step = 'planned';
		await store.writeUsbExport();
		const result = { step: store.usbExport.step, code: store.usbExport.problem?.code ?? null, message: store.usbExport.problem?.message ?? null };
		// What a healthy scan that no longer lists the stick leaves on screen.
		store.forgetVanishedTarget();
		return { ...result, afterVanish: store.usbExport.problem?.code ?? null };
	} finally {
		await new Promise((resolve) => server.close(resolve));
	}
}

test('only a literal verified: true on both answers reads as verified (Codex 4350696747)', async () => {
	for (const [apply, readback] of [[false, true], [true, false], [true, 'false'], [true, undefined]]) {
		const read = await writeThenRead(apply, readback);
		assert.deepEqual([read.step, read.code], ['edit', 'readback_unverified'], `${apply} / ${readback}`);
	}
	// A receipt whose `verified` is not a boolean is malformed and never read
	// back (Codex 4350964742), so it cannot reach 'done' either.
	for (const apply of ['false', 1, undefined]) {
		const read = await writeThenRead(apply, true);
		assert.deepEqual([read.step, read.code, read.afterVanish], ['edit', 'written_receipt_malformed', 'written_receipt_malformed'], `${apply}`);
	}
	// Control: both true is the verified result.
	assert.deepEqual(await writeThenRead(true, true), { step: 'done', code: null, message: null, afterVanish: null });
});

test('an apply receipt that is not ReceiptModel\'s is never forwarded to /readback and retires the plan (Codex 4350964742)', async () => {
	const without = (key) => Object.fromEntries(Object.entries(RECEIPT).filter(([k]) => k !== key));
	const broken = {
		'plan_id (missing)': [without('plan_id'), 'plan_id'],
		'volume_uuid (number)': [{ ...RECEIPT, volume_uuid: 4 }, 'volume_uuid'],
		'output_sha256 (missing)': [without('output_sha256'), 'output_sha256'],
		'output_relative_path (null)': [{ ...RECEIPT, output_relative_path: null }, 'output_relative_path'],
		'playlist_ids (strings)': [{ ...RECEIPT, playlist_ids: ['1'] }, 'playlist_ids'],
		'playlist_ids (missing)': [without('playlist_ids'), 'playlist_ids'],
		'not an object': [[], 'the receipt']
	};
	for (const [label, [receipt, field]] of Object.entries(broken)) {
		const seen = [];
		const read = await writeThenRead(true, true, READBACK, receipt, seen);
		assert.deepEqual([read.step, read.code], ['edit', 'written_receipt_malformed'], label);
		assert.ok((read.message ?? '').includes(field), `${label}: ${read.message}`);
		// The write ran, so the warning outlives the stick leaving (Codex 4351287674).
		assert.equal(read.afterVanish, 'written_receipt_malformed', `${label}: the warning was dropped when the stick left`);
		assert.deepEqual(seen, ['/api/v1/usb-export/apply'], `${label}: the malformed receipt was forwarded`);
	}
	// Control: a contract-valid receipt is forwarded and the write verifies, and
	// an export with no playlists (an empty playlist_ids) is still well formed.
	const seen = [];
	assert.equal((await writeThenRead(true, true, READBACK, RECEIPT, seen)).step, 'done');
	assert.deepEqual(seen, ['/api/v1/usb-export/apply', '/api/v1/usb-export/readback']);
	assert.equal((await writeThenRead(true, true, READBACK, { ...RECEIPT, playlist_ids: [] })).step, 'done');
});

test('a verified read-back whose other fields are not ReadbackModel\'s is not shown as verified (Codex 4350837630)', async () => {
	const without = (key) => Object.fromEntries(Object.entries(READBACK).filter(([k]) => k !== key));
	const broken = {
		'playlists (string)': [{ ...READBACK, playlists: 'oops' }, 'playlists'],
		'playlists (missing)': [without('playlists'), 'playlists'],
		'playlists (row not an object)': [{ ...READBACK, playlists: ['Friday'] }, 'playlists'],
		'track_updates (missing)': [without('track_updates'), 'track_updates'],
		'plan_id (number)': [{ ...READBACK, plan_id: 1 }, 'plan_id'],
		'output_sha256 (missing)': [without('output_sha256'), 'output_sha256'],
		'volume_uuid (null)': [{ ...READBACK, volume_uuid: null }, 'volume_uuid'],
		'output_relative_path (missing)': [without('output_relative_path'), 'output_relative_path']
	};
	for (const [label, [answer, field]] of Object.entries(broken)) {
		const read = await writeThenRead(true, true, answer);
		assert.deepEqual([read.step, read.code], ['edit', 'readback_unverified'], label);
		assert.match(read.message ?? '', new RegExp(`malformed \\(.*${field}`), label);
	}
	// Control: the full answer verifies, and an empty playlist list is still well formed.
	assert.equal((await writeThenRead(true, true, READBACK)).step, 'done');
	assert.equal((await writeThenRead(true, true, { ...READBACK, playlists: [] })).step, 'done');
});

test('a verified read-back of another write is not this write\'s proof (Codex 4351495306)', async () => {
	const other = {
		'plan_id (readback)': [{ ...READBACK, plan_id: 'p-2' }, RECEIPT, 'plan_id'],
		'volume_uuid (readback)': [{ ...READBACK, volume_uuid: 'other-uuid' }, RECEIPT, 'volume_uuid'],
		'output_relative_path (readback)': [{ ...READBACK, output_relative_path: 'elsewhere.db' }, RECEIPT, 'output_relative_path'],
		'output_sha256 (readback)': [{ ...READBACK, output_sha256: '1'.repeat(64) }, RECEIPT, 'output_sha256'],
		'plan_id (receipt and readback)': [{ ...READBACK, plan_id: 'p-2' }, { ...RECEIPT, plan_id: 'p-2' }, 'plan_id'],
		'volume_uuid (receipt)': [READBACK, { ...RECEIPT, volume_uuid: 'other-uuid' }, 'volume_uuid'],
		'output_sha256 (receipt)': [READBACK, { ...RECEIPT, output_sha256: '1'.repeat(64) }, 'output_sha256']
	};
	for (const [label, [readback, receipt, field]] of Object.entries(other)) {
		const read = await writeThenRead(true, true, readback, receipt);
		assert.deepEqual([read.step, read.code], ['edit', 'readback_unverified'], label);
		assert.match(read.message ?? '', new RegExp(`another write \\(${field} differ`), label);
	}
	// Control: when the plan, receipt and read-back agree, the write verifies.
	assert.equal((await writeThenRead(true, true, READBACK, RECEIPT)).step, 'done');
});

async function ejectDaemon({ busy = false, answerOverride = undefined } = {}) {
	const answer = answerOverride ?? readFileSync(new URL('./fixtures/usb-eject-ok.json', import.meta.url), 'utf8');
	const refusal = readFileSync(new URL('./fixtures/usb-eject-busy.json', import.meta.url), 'utf8');
	const seen = [];
	const held = [];
	const server = createServer((req, res) => {
		let body = '';
		req.on('data', (chunk) => (body += chunk));
		req.on('end', () => {
			seen.push({ method: req.method, url: req.url, body });
			if (req.method === 'POST' && req.url === '/api/v1/usb/eject') {
				res.writeHead(busy ? 409 : 200, { 'content-type': 'application/json' });
				res.end(busy ? refusal : answer);
			} else if (req.method === 'GET' && req.url?.startsWith('/api/v1/usb/volumes')) {
				held.push(res);
			} else {
				res.writeHead(404, { 'content-type': 'application/json' });
				res.end('{"detail":"not served by this double"}');
			}
		});
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	const base = `http://127.0.0.1:${server.address().port}`;
	const close = async () => {
		for (const res of held) {
			res.writeHead(503, { 'content-type': 'application/json' });
			res.end('{"detail":"released by the test"}');
		}
		await new Promise((resolve) => server.close(resolve));
	};
	return { base, seen, held, close, answer: JSON.parse(answer) };
}

test('a 200 eject answer without a literal ejected: true never says the stick is safe to pull (Codex 4350837641)', async () => {
	const ok = JSON.parse(readFileSync(new URL('./fixtures/usb-eject-ok.json', import.meta.url), 'utf8'));
	const answers = {
		'ejected false': { ...ok, ejected: false },
		'ejected "true"': { ...ok, ejected: 'true' },
		'no ejected': Object.fromEntries(Object.entries(ok).filter(([k]) => k !== 'ejected')),
		'no name': Object.fromEntries(Object.entries(ok).filter(([k]) => k !== 'name')),
		'no message': Object.fromEntries(Object.entries(ok).filter(([k]) => k !== 'message')),
		'no mount_path': Object.fromEntries(Object.entries(ok).filter(([k]) => k !== 'mount_path')),
		// A success for another mount is not this stick's (Codex 4351947909).
		'another mount_path': { ...ok, mount_path: '/Volumes/GIG-B' },
		'empty object': {},
		'array': []
	};
	for (const [label, body] of Object.entries(answers)) {
		const daemon = await ejectDaemon({ answerOverride: JSON.stringify(body) });
		try {
			const store = await loadTypeScriptModule('src/lib/rb/usb-export.svelte.ts', { viteApiBase: daemon.base });
			const id = `vol:${UUID}`;
			store.usbExport.open = true;
			store.usbExport.targetPath = '/Volumes/GIG-A';
			store.usbExport.targetId = id;
			await store.ejectUsbStick('/Volumes/GIG-A', id);
			assert.equal(store.usbExport.ejectNotice, null, label);
			assert.equal(store.usbExport.ejectWatch, null, label);
			assert.equal(store.usbExport.ejectProblem?.code, 'eject_unconfirmed', label);
			assert.match(store.usbExport.ejectProblem?.message ?? '', /Keep the stick plugged in/, label);
			// The pick stays and the slot is free; no rescan is started for a stick still mounted.
			assert.equal(store.usbExport.targetId, id, label);
			assert.equal(store.usbExport.ejecting, null, label);
			assert.equal(daemon.seen.some((r) => r.method === 'GET'), false, label);
		} finally {
			await daemon.close();
		}
	}
});

test('a successful eject keeps the write warning and readback, sets the eject watch, and frees the slot before the rescan (Codex P2 4300993872, 4287034882)', async () => {
	const daemon = await ejectDaemon();
	try {
		const store = await loadTypeScriptModule('src/lib/rb/usb-export.svelte.ts', { viteApiBase: daemon.base });
		const id = `vol:${UUID}`;
		store.usbExport.open = true;
		store.usbExport.targetPath = '/Volumes/GIG-A';
		store.usbExport.targetId = id;
		store.usbExport.plan = { plan_id: 'p-1', volume_label: 'GIG-A' };
		store.usbExport.readback = { verified: true };
		store.usbExport.step = 'done';
		store.usbExport.problem = { code: 'write_outcome_unknown', message: 'The write may not have finished.' };

		// The daemon never answers the rescan, so an eject that waited on it would never return.
		let timer;
		const outcome = await Promise.race([
			store.ejectUsbStick('/Volumes/GIG-A', id).then(() => 'returned'),
			new Promise((resolve) => (timer = setTimeout(() => resolve('stalled'), 5000)))
		]);
		clearTimeout(timer);
		assert.equal(outcome, 'returned', 'the eject waited on the held rescan, so a stalled scan locks the screen');

		const eject = daemon.seen.find((r) => r.method === 'POST' && r.url === '/api/v1/usb/eject');
		assert.ok(eject, 'the eject went to the daemon');
		assert.deepEqual(JSON.parse(eject.body), { mount_path: '/Volumes/GIG-A', volume_id: id });
		assert.equal(store.usbExport.ejectNotice, `${daemon.answer.name} ejected. You can pull it out.`);
		assert.equal(store.usbExport.ejectProblem, null);
		assert.deepEqual(store.usbExport.ejectWatch, { id, others: [], sinceSeq: 0, afterSeq: 0 });
		// The ejected stick stops being the target, but the warning the stick may
		// need and the verified readback survive (an eject is not a plan reset).
		assert.equal(store.usbExport.targetPath, null);
		assert.equal(store.usbExport.targetId, null);
		assert.equal(store.usbExport.problem?.code, 'write_outcome_unknown');
		assert.deepEqual(store.usbExport.readback, { verified: true });
		assert.equal(store.usbExport.step, 'done');
		assert.equal(store.usbExport.plan?.volume_label, 'GIG-A');
		// The rescan was started, and is still held open by the daemon, yet the
		// eject has returned and the slot is free: a stalled scan cannot lock the screen.
		await new Promise((resolve) => {
			const poll = () => (daemon.held.length > 0 ? resolve() : setTimeout(poll, 5));
			poll();
		});
		assert.equal(store.usbExport.busy, null);
		assert.equal(store.usbExport.ejecting, null);
		// Control: with the slot free, the next operation is not refused as busy.
		assert.equal(store.openUsbExportForPlaylist('Peak Time'), true);
	} finally {
		await daemon.close();
	}
});

test('a refused eject keeps the target and names the problem (the opposite direction)', async () => {
	const daemon = await ejectDaemon({ busy: true });
	try {
		const store = await loadTypeScriptModule('src/lib/rb/usb-export.svelte.ts', { viteApiBase: daemon.base });
		store.usbExport.targetPath = '/Volumes/GIG-A';
		store.usbExport.targetId = `vol:${UUID}`;
		await store.ejectUsbStick('/Volumes/GIG-A', `vol:${UUID}`);
		assert.equal(store.usbExport.ejectProblem?.code, 'eject_volume_busy', 'a refused eject must be named');
		assert.equal(store.usbExport.ejectNotice, null);
		assert.equal(store.usbExport.ejectWatch, null);
		assert.equal(store.usbExport.targetPath, '/Volumes/GIG-A', 'a failed eject keeps the target');
		assert.equal(daemon.held.length, 0, 'no rescan after a failed eject');
		assert.equal(store.usbExport.busy, null);
	} finally {
		await daemon.close();
	}
});

/** A daemon double that holds the eject until the test releases it and serves
 * the volume listings in `listings` in turn, holding any request past them. */
async function racingEjectDaemon(listings) {
	const fixture = (name) => readFileSync(new URL(`./fixtures/${name}`, import.meta.url), 'utf8');
	const answer = fixture('usb-eject-ok.json');
	const queue = listings.map(fixture);
	const held = [];
	let ejectRes = null;
	let ejectSeen;
	const ejectArrived = new Promise((resolve) => (ejectSeen = resolve));
	const server = createServer((req, res) => {
		req.on('data', () => {});
		req.on('end', () => {
			if (req.method === 'POST' && req.url === '/api/v1/usb/eject') {
				ejectRes = res;
				ejectSeen();
			} else if (req.method === 'GET' && req.url?.startsWith('/api/v1/usb/volumes') && queue.length > 0) {
				res.writeHead(200, { 'content-type': 'application/json' });
				res.end(queue.shift());
			} else if (req.method === 'GET' && req.url?.startsWith('/api/v1/usb/volumes')) {
				held.push(res);
			} else {
				res.writeHead(404, { 'content-type': 'application/json' });
				res.end('{"detail":"not served by this double"}');
			}
		});
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	const release = () => {
		ejectRes.writeHead(200, { 'content-type': 'application/json' });
		ejectRes.end(answer);
	};
	const close = async () => {
		for (const res of held) {
			res.writeHead(503, { 'content-type': 'application/json' });
			res.end('{"detail":"released by the test"}');
		}
		await new Promise((resolve) => server.close(resolve));
	};
	return { base: `http://127.0.0.1:${server.address().port}`, ejectArrived, release, close };
}

async function ejectRacedBy(secondListing) {
	const daemon = await racingEjectDaemon(['usb-volumes-gig-a.json', secondListing]);
	try {
		const both = await loadTypeScriptModule('tests/unit/fixtures/usb-export-tracker-entry.ts', { viteApiBase: daemon.base });
		await both.refreshUsbVolumes();
		assert.deepEqual(both.exportTargets(both.usbTracker.volumes, both.usbTracker).map((v) => v.id), [PICKED.id], 'the stick is listed before the eject');
		const ejecting = both.ejectUsbStick(PICKED.mount_path, PICKED.id);
		await daemon.ejectArrived;
		// A refresh started while the eject runs lands before its answer.
		await both.refreshUsbVolumes();
		daemon.release();
		await ejecting;
		return { notice: both.usbExport.ejectNotice, watch: both.usbExport.ejectWatch };
	} finally {
		await daemon.close();
	}
}

test('a replacement listed by a refresh that ran during the eject ends the notice (Sol P1 4349545524)', async () => {
	const raced = await ejectRacedBy('usb-volumes-replacement.json');
	assert.equal(raced.watch, null, 'the replacement joined the sticks already listed, or its listing counted as too old');
	assert.equal(raced.notice, null);
	// Control: the same race with the stick simply gone keeps the notice.
	const gone = await ejectRacedBy('usb-volumes-none.json');
	assert.match(gone.notice ?? '', /ejected\. You can pull it out\.$/);
	assert.deepEqual(gone.watch, { id: PICKED.id, others: [], sinceSeq: 1, afterSeq: 2 });
});

/** A daemon double that answers GET /api/v1/usb/volumes with `bodies` in turn. */
async function listingDaemon(bodies) {
	const queue = [...bodies];
	const server = createServer((req, res) => {
		req.on('data', () => {});
		req.on('end', () => {
			if (req.method === 'GET' && req.url?.startsWith('/api/v1/usb/volumes') && queue.length > 0) {
				res.writeHead(200, { 'content-type': 'application/json' });
				res.end(queue.shift());
			} else {
				res.writeHead(404, { 'content-type': 'application/json' });
				res.end('{"detail":"not served by this double"}');
			}
		});
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	return { base: `http://127.0.0.1:${server.address().port}`, close: () => new Promise((resolve) => server.close(resolve)) };
}

async function listedThen(second) {
	const first = readFileSync(new URL('./fixtures/usb-volumes-gig-a.json', import.meta.url), 'utf8');
	const daemon = await listingDaemon([first, second]);
	try {
		const both = await loadTypeScriptModule('tests/unit/fixtures/usb-export-tracker-entry.ts', { viteApiBase: daemon.base });
		await both.refreshUsbVolumes();
		assert.equal(both.usbTracker.discovery, 'ok');
		await both.refreshUsbVolumes();
		const stick = both.usbTracker.volumes.find((v) => v.id === PICKED.id);
		return { discovery: both.usbTracker.discovery, lastError: both.usbTracker.lastError, present: stick?.present ?? null };
	} finally {
		await daemon.close();
	}
}

test('a 200 volume listing with no volumes is a discovery error, never an empty healthy list (Sol P1 4350275754)', async () => {
	for (const body of ['{"scanned_at": 1002.0, "watching": true}', '{"volumes": null, "scanned_at": 1002.0}', '{"volumes": [{}], "scanned_at": 1002.0}', '{"volumes": []}']) {
		const read = await listedThen(body);
		assert.equal(read.discovery, 'unreachable', body);
		assert.match(read.lastError ?? '', /USB volume answer is malformed: (volumes|volumes\[\]\.id|scanned_at)/, body);
		assert.equal(read.present, true, `a malformed answer must not mark the stick gone: ${body}`);
	}
	// Control: a well-formed empty listing still says the stick left.
	const gone = await listedThen(readFileSync(new URL('./fixtures/usb-volumes-none.json', import.meta.url), 'utf8'));
	assert.equal(gone.discovery, 'ok');
	assert.equal(gone.present, false);
});

test('a 200 listing row with a kind, role or access outside the daemon\'s enums is a discovery error (Codex 4351111102)', async () => {
	const full = JSON.parse(readFileSync(new URL('./fixtures/usb-volumes-gig-a.json', import.meta.url), 'utf8')).volumes[0];
	const listing = (row) => JSON.stringify({ volumes: [row], scanned_at: 1002.0, watching: true });
	// The allowed values come from the daemon's own contract, so a value it adds
	// that the client does not know fails here rather than reading as an unplug.
	const openapi = JSON.parse(readFileSync(new URL('../../../openapi.json', import.meta.url), 'utf8'));
	const props = openapi.components.schemas.UsbVolumeOut.properties;
	for (const field of ['kind', 'role', 'access']) {
		assert.ok(Array.isArray(props[field].enum) && props[field].enum.length > 0, `${field} is an enum in openapi.json`);
		for (const value of props[field].enum) {
			const read = await listedThen(listing({ ...full, [field]: value }));
			assert.equal(read.discovery, 'ok', `${field}: ${value} is the daemon's own value`);
		}
		const read = await listedThen(listing({ ...full, [field]: `${props[field].enum[0]}-typo` }));
		assert.equal(read.discovery, 'unreachable', `${field} typo`);
		assert.match(read.lastError ?? '', new RegExp(`malformed: .*volumes\\[\\]\\.${field}`), `${field} typo`);
		assert.equal(read.present, true, `a ${field} typo must not mark the stick gone`);
	}
	// A typo'd role is the reported case: it must not drop the picked stick.
	const read = await listedThen(listing({ ...full, role: 'usb-stick' }));
	assert.equal(read.discovery, 'unreachable');
	assert.equal(read.present, true);
});

test('a 200 listing whose row lacks or mistypes a field the daemon always sends is a discovery error (Codex 4350753095)', async () => {
	const full = JSON.parse(readFileSync(new URL('./fixtures/usb-volumes-gig-a.json', import.meta.url), 'utf8')).volumes[0];
	const listing = (row) => JSON.stringify({ volumes: [row], scanned_at: 1002.0, watching: true });
	const without = (key) => Object.fromEntries(Object.entries(full).filter(([k]) => k !== key));
	const broken = {
		present: without('present'),
		'present (string)': { ...full, present: 'true' },
		name: without('name'),
		kind: without('kind'),
		simulated: without('simulated'),
		is_music: { ...full, is_music: null },
		role: without('role'),
		protocol: { ...full, protocol: 1 },
		access: { ...full, access: null },
		'role (typo)': { ...full, role: 'usb-stick' },
		mount_path: { ...full, mount_path: false }
	};
	for (const [label, row] of Object.entries(broken)) {
		const field = label.split(' ')[0];
		const read = await listedThen(listing(row));
		assert.equal(read.discovery, 'unreachable', label);
		assert.match(read.lastError ?? '', new RegExp(`malformed: .*volumes\\[\\]\\.${field}`), label);
		assert.equal(read.present, true, `a malformed row must not mark the stick gone: ${label}`);
	}
	// Controls: the full row reads, and so does an older daemon's row without
	// the optional protocol, hide_reason and access fields (nulls allowed where the model allows them).
	for (const [label, row] of [
		['full row', full],
		['older daemon', without('access')],
		['no protocol or hide_reason', Object.fromEntries(Object.entries(full).filter(([k]) => k !== 'protocol' && k !== 'hide_reason'))],
		['null mount_path', { ...full, mount_path: null }]
	]) {
		const read = await listedThen(listing(row));
		assert.equal(read.discovery, 'ok', label);
		assert.equal(read.present, true, label);
	}
});

async function planDaemon(plan) {
	const server = createServer((req, res) => {
		req.on('data', () => {});
		req.on('end', () => {
			if (req.method === 'POST' && req.url === '/api/v1/usb-export/plan') {
				res.writeHead(200, { 'content-type': 'application/json' });
				res.end(JSON.stringify(plan));
			} else {
				res.writeHead(404, { 'content-type': 'application/json' });
				res.end('{"detail":"not served by this double"}');
			}
		});
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	return { base: `http://127.0.0.1:${server.address().port}`, close: () => new Promise((resolve) => server.close(resolve)) };
}

async function planAnswered(plan) {
	const daemon = await planDaemon(plan);
	try {
		const store = await loadTypeScriptModule('src/lib/rb/usb-export.svelte.ts', { viteApiBase: daemon.base });
		Object.assign(store.usbExport, {
			open: true,
			targetPath: PICKED.mount_path,
			targetId: PICKED.id,
			templatePath: '/opt/odj-fixture/template/exportLibrary.db',
			playlistName: 'Friday',
			trackIdsText: '1, 2'
		});
		await store.planUsbExport();
		return { plan: store.usbExport.plan, problem: store.usbExport.problem, step: store.usbExport.step };
	} finally {
		await daemon.close();
	}
}

test('a plan answer whose playlist omits track_ids is refused, not shown as empty and armed for Write (Sol P1 4349545535)', async () => {
	// Synthesized, not captured from a run (`plan-1` is no SHA-256 plan id and the
	// template path is invented); (private path) pins only
	// that PlanModel round-trips it unchanged (Codex 4350837649).
	const synthesized = JSON.parse(readFileSync(new URL('./fixtures/usb-plan-gig-a.json', import.meta.url), 'utf8'));
	const { track_ids: _dropped, ...noIds } = synthesized.playlists[0];
	const drifted = await planAnswered({ ...synthesized, playlists: [noIds] });
	assert.equal(drifted.plan, null);
	assert.equal(drifted.step, 'edit');
	assert.equal(drifted.problem?.code, 'daemon_envelope_malformed');
	assert.match(drifted.problem?.message ?? '', /playlists\[0\]\.track_ids/);
	// Control: the model-shaped answer is stored and armed.
	const whole = await planAnswered(synthesized);
	assert.equal(whole.problem, null);
	assert.equal(whole.step, 'planned');
	assert.deepEqual(whole.plan, synthesized);
	// The screen renders the checked count, with no fallback that would hide drift.
	const screen = readFileSync(new URL('../../src/lib/components/rb/UsbExportScreen.svelte', import.meta.url), 'utf8');
	assert.match(screen, /\$\{p\.name\} \(\$\{p\.track_ids\.length\}\)/);
	assert.doesNotMatch(screen, /track_ids \?\? \[\]/);
});

test('a plan answer for the picked UUID at another mount is refused, not armed for Write (Codex 4352307431)', async () => {
	// Synthesized, not captured from a run; see the fixture note above.
	const synthesized = JSON.parse(readFileSync(new URL('./fixtures/usb-plan-gig-a.json', import.meta.url), 'utf8'));
	const elsewhere = await planAnswered({ ...synthesized, target_root: '/Volumes/GIG-A 1' });
	assert.equal(elsewhere.plan, null);
	assert.equal(elsewhere.step, 'edit');
	assert.equal(elsewhere.problem?.code, 'target_changed');
	// Control: the same answer at the picked mount is stored and armed.
	assert.equal(synthesized.target_root, PICKED.mount_path);
	const here = await planAnswered(synthesized);
	assert.equal(here.problem, null);
	assert.equal(here.step, 'planned');
});

test('the plan envelope check refuses any field PlanModel requires, missing or mistyped (Codex 4349817101)', () => {
	// Synthesized, not captured from a run (`plan-1` is no SHA-256 plan id and the
	// template path is invented); (private path) pins only
	// that PlanModel round-trips it unchanged (Codex 4350837649).
	const synthesized = JSON.parse(readFileSync(new URL('./fixtures/usb-plan-gig-a.json', import.meta.url), 'utf8'));
	// Control: the model-shaped answer passes, so every refusal below is the field's.
	assert.equal(mod.checkPlanEnvelope(synthesized), synthesized);
	const required = [
		'plan_id', 'schema_version', 'scope', 'template_path', 'template_sha256', 'target_root',
		'volume_label', 'volume_uuid', 'authorization_id', 'output_relative_path', 'playlists', 'track_updates'
	];
	for (const key of required) {
		const { [key]: _gone, ...rest } = synthesized;
		const problem = mod.checkPlanEnvelope(rest);
		assert.equal(problem.code, 'daemon_envelope_malformed', key);
		assert.match(problem.message, new RegExp(`: (.*, )?${key}(,|$)`), key);
		assert.equal(mod.checkPlanEnvelope({ ...synthesized, [key]: key === 'schema_version' ? '1' : 7 }).code, 'daemon_envelope_malformed', `${key} mistyped`);
	}
	const update = synthesized.track_updates[0];
	for (const [field, value] of [['id', '1'], ['rating', '3'], ['dj_comment', 4], ['color_id', 1.5]]) {
		const problem = mod.checkPlanEnvelope({ ...synthesized, track_updates: [{ ...update, [field]: value }] });
		assert.match(problem.message ?? '', new RegExp(`track_updates\\[0\\]\\.${field}`), field);
	}
	assert.match(mod.checkPlanEnvelope({ ...synthesized, playlists: [{ ...synthesized.playlists[0], parent_id: 'x' }] }).message, /playlists\[0\]\.parent_id/);
	// Optional fields may be absent or null, as the daemon sends them.
	const { title: _t, ...noTitle } = update;
	assert.equal(mod.checkPlanEnvelope({ ...synthesized, track_updates: [noTitle] }).plan_id, synthesized.plan_id);
	assert.equal(mod.checkPlanEnvelope(null).code, 'daemon_envelope_malformed');
});

test('every field the daemon models require is checked on the client, read from openapi.json, not a copy kept by hand', () => {
	// The client checks plan, receipt and read-back shapes so a drifted answer
	// is refused before it is rendered or sent back (Sol P1 4349545535, Codex
	// 4349817101, 4350964742, 4350837630). Those checks list fields by hand, so
	// this pins them to the generated contract: a field PlanModel, ReceiptModel,
	// ReadbackModel, PlaylistModel or TrackUpdateModel newly requires fails here
	// until the client checks it too (nightly code-quality review 4355739329).
	const schemas = JSON.parse(readFileSync(new URL('../../../openapi.json', import.meta.url), 'utf8')).components.schemas;
	const required = (name) => {
		const list = schemas[name]?.required;
		assert.ok(Array.isArray(list) && list.length > 0, `${name} names its required fields in openapi.json`);
		return list;
	};
	// Positive control that the parser read the real contract.
	assert.ok(required('PlanModel').includes('plan_id'));
	const synthesized = JSON.parse(readFileSync(new URL('./fixtures/usb-plan-gig-a.json', import.meta.url), 'utf8'));
	assert.equal(mod.checkPlanEnvelope(synthesized), synthesized, 'control: the whole plan passes');
	for (const key of required('PlanModel')) {
		const { [key]: _gone, ...rest } = synthesized;
		assert.match(mod.checkPlanEnvelope(rest).message ?? '', new RegExp(`(: |, )${key}(,|$)`), `PlanModel.${key}`);
	}
	for (const key of required('PlaylistModel')) {
		const { [key]: _gone, ...pl } = synthesized.playlists[0];
		assert.match(mod.checkPlanEnvelope({ ...synthesized, playlists: [pl] }).message ?? '', new RegExp(`playlists\\[0\\]\\.${key}`), `PlaylistModel.${key}`);
	}
	for (const key of required('TrackUpdateModel')) {
		const { [key]: _gone, ...u } = synthesized.track_updates[0];
		assert.match(mod.checkPlanEnvelope({ ...synthesized, track_updates: [u] }).message ?? '', new RegExp(`track_updates\\[0\\]\\.${key}`), `TrackUpdateModel.${key}`);
	}
	assert.deepEqual(mod.malformedReceiptFields(RECEIPT), [], 'control: the whole receipt passes');
	for (const key of required('ReceiptModel')) {
		const { [key]: _gone, ...rest } = RECEIPT;
		assert.deepEqual(mod.malformedReceiptFields(rest), [key], `ReceiptModel.${key}`);
	}
	assert.deepEqual(mod.malformedReadbackFields(READBACK), [], 'control: the whole read-back passes');
	for (const key of required('ReadbackModel')) {
		const { [key]: _gone, ...rest } = READBACK;
		assert.deepEqual(mod.malformedReadbackFields(rest), [key], `ReadbackModel.${key}`);
	}
});
