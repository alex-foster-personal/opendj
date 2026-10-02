/**
 * The setup OVERLAY: its open/collapsed/incomplete rules, and the detect
 * step's severity rules.
 *
 * ORIGIN (the maintainer, MacBook Air, Wed 19 Aug 2026). The detect step read as
 * "failed to find rekordbox library", in grey, with nothing enabled to press
 * next -- on a machine whose engine log showed detection answering 200 three
 * times and the import afterwards succeeding with 8558 tracks. Two directives
 * came out of it: failure text must be RED, and a next step must ALWAYS be
 * available. Both are pinned here.
 *
 * The rules are pure functions plus one plain store, executed for real. Only
 * the markup facts a node:test harness cannot render are pinned by source
 * shape, the way capability-gating-markup.test.mjs pins its own.
 *
 * Regression lines:
 * - if a null detection that is still idle or scanning reports phase
 *   'answered' then the not-found visual can be painted before an answer
 *   exists, which is the whole bug
 * - if a failed detection reports phase 'scanning' then the scanning sentence
 *   renders beside the red error (issue #3422)
 * - if every absent probe reads red then the colour stops meaning "this is
 *   why nothing can be imported" and starts meaning "a file is missing"
 * - if a fatal blocker renders in the muted tone then a sentence that stops
 *   the import looks like one that does not
 * - if any escape action becomes conditional then some detection result
 *   dead-ends the step
 * - if collapse clears `open` then minimising a running import loses it
 * - if the refusal is only a hover title then a trackpad user never sees why
 *   the button is dead
 * - if a legacy daemon stops making the surface inert then setup fires at an
 *   endpoint guaranteed to 404
 * - if an UNFINISHED probe makes the surface inert then a healthy engine is
 *   reported as a failed setup one tick after load
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { engineHealth, jsonResponse } from './setup-fixtures.mjs';

const API_BASE = 'https://setup-overlay.example.test';

function read(relative) {
	return readFileSync(fileURLToPath(new URL(`../../${relative}`, import.meta.url)), 'utf8');
}

function fileProbe(path, exists = true) {
	return { path, exists, size_bytes: exists ? 1024 : null, modified_at: null };
}

/** A HEALTHY machine: the plain copy is there, so the absent working copy is
 * not a problem and must not be painted as one. */
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

/** Nothing to read anywhere. This is the state that must be unmistakable. */
function nothingFound() {
	return detection({
		installed: false,
		live_db: fileProbe('/Users/dj/Library/Pioneer/rekordbox/master.db', false),
		share_dir: fileProbe('/Users/dj/Library/Pioneer/rekordbox/share', false),
		working_copy: fileProbe('/data/master.db.copy', false),
		plain_copy: fileProbe('/data/master.plain.db', false),
		import_source: null,
		import_source_encrypted: null,
		blockers: ['rekordbox_not_found', 'rekordbox_share_missing']
	});
}

let mod;
let originalFetch;

before(async () => {
	mod = await loadTypeScriptModule('tests/unit/fixtures/setup-overlay-entry.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	globalThis.fetch = originalFetch;
	mod._resetSetupOverlayForTests();
});

// ------------------------------------------------------------- the phase

test('a null detection is SCANNING while idle, never a verdict', () => {
	// The bug: null meant "never asked", "asking now" and "asked and failed"
	// all at once, and the step painted all three as one grey sentence that
	// nothing would ever clear. Idle and scanning stay in flight.
	assert.equal(mod.detectPhase(null, 'idle'), 'scanning');
	assert.equal(mod.detectPhase(null, 'scanning'), 'scanning');
});

test('a failed detection is never SCANNING, even with a null answer', () => {
	// [if] detectState is failed [then] detectPhase does not return scanning,
	// [else stop].
	assert.equal(mod.detectPhase(null, 'failed'), 'answered');
	assert.equal(mod.detectPhase(detection(), 'failed'), 'answered');
});

test('the detect step passes detectState, and Done waits on a status refresh', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /detectPhase\(detection, detectState\)/);
	assert.doesNotMatch(overlay, /detectPhase\(detection, detectState === 'scanning'\)/);
	assert.match(overlay, /refreshStatusAfterImport\(job\.id\)/);
	assert.match(overlay, /statusRefreshJobId: setupWizard\.statusRefreshJobId/);
});

test('a re-ask is scanning too, even with a previous answer on screen', () => {
	assert.equal(mod.detectPhase(detection(), 'scanning'), 'scanning');
	assert.equal(mod.detectPhase(detection(), 'answered'), 'answered');
});

test('the scanning sentence says what is being looked for', () => {
	// It must not be readable as "we looked and found nothing".
	assert.match(mod.SCANNING_SENTENCE, /Looking for/);
	assert.match(mod.SCANNING_SENTENCE, /\.\.\.$/);
});

// ------------------------------------------------------------- the colour

test('a missing file is red only when it is why nothing can be imported', () => {
	const healthy = mod.probeRows(detection());
	const workingCopy = healthy.find((row) => row.key === 'working_copy');
	assert.equal(workingCopy.danger, false, 'an absent copy with a usable plain copy is fine');
	assert.match(workingCopy.text, /not present at/);

	const broken = mod.probeRows(nothingFound());
	assert.equal(broken.find((row) => row.key === 'live_db').danger, true);
	assert.equal(broken.find((row) => row.key === 'working_copy').danger, true);
	assert.equal(broken.find((row) => row.key === 'plain_copy').danger, true);
});

test('the analysis folder is never the red line', () => {
	// Tracks land without it; only waveforms are missing. Painting it red
	// would teach the operator to distrust the colour.
	const rows = mod.probeRows(nothingFound());
	assert.equal(rows.find((row) => row.key === 'share_dir').danger, false);
});

test('an unreachable key is red on its own line', () => {
	const rows = mod.probeRows(
		detection({
			key_available: false,
			key_detail: 'the sqlcipher3 driver is not installed',
			import_source_encrypted: true,
			blockers: ['rekordbox_key_unavailable']
		})
	);
	const key = rows.find((row) => row.key === 'key');
	assert.equal(key.danger, true);
	assert.match(key.text, /sqlcipher3/);
});

test('every probe line carries its hover explanation', () => {
	// House rule: a readout says what it is.
	for (const row of mod.probeRows(detection())) {
		assert.ok(row.title.length > 20, `${row.key} has no real title`);
	}
});

test('a blocker that stops the import is danger; one that does not is not', () => {
	assert.equal(mod.blockerTone('rekordbox_not_found'), 'danger');
	assert.equal(mod.blockerTone('rekordbox_key_unavailable'), 'danger');
	assert.equal(mod.blockerTone('rekordbox_share_missing'), 'warning');
	// The tone can never disagree with the gate that refuses the import.
	for (const code of ['rekordbox_not_found', 'rekordbox_key_unavailable', 'rekordbox_share_missing']) {
		assert.equal(mod.blockerTone(code) === 'danger', mod.isFatalBlocker(code), code);
	}
});

// -------------------------------------------------------- the escape hatch

test('the detect step always offers three ways forward', () => {
	// A constant, not a derivation: there is no detection result that removes
	// any of these, so there is no state in which the step dead-ends.
	assert.deepEqual(
		mod.ESCAPE_ACTIONS.map((action) => action.id),
		['redetect', 'folder', 'dismiss']
	);
	assert.equal(mod.ESCAPE_ACTIONS[0].label, 'Look again');
	assert.equal(mod.ESCAPE_ACTIONS[1].label, 'Choose a folder instead');
	assert.equal(mod.ESCAPE_ACTIONS[2].label, 'Continue without importing');
});

test('every escape action names the endpoint it drives', () => {
	// Agent-native parity: a control a human can press is a request an agent
	// can make, and the tooltip is where that is written down.
	assert.match(mod.ESCAPE_ACTIONS[0].title, /\/api\/v1\/setup\/detect\/rekordbox/);
	assert.match(mod.ESCAPE_ACTIONS[1].title, /\/api\/v1\/setup\/detect\/folder/);
	assert.match(mod.ESCAPE_ACTIONS[2].title, /\/api\/v1\/setup\/dismiss/);
});

test('a fatal blocker refuses Continue and says so in words', () => {
	// The refusal string is what the overlay renders INLINE beside the
	// buttons. An empty or generic one would be the hover-title bug again.
	const refusal = mod.advanceRefusal('detect', {
		source: 'rekordbox',
		detection: nothingFound(),
		folderRows: [{ id: 'row-1', path: '', scan: null }],
		job: null
	});
	assert.match(refusal, /rekordbox_not_found/);
	// The folder branch must NOT inherit a rekordbox blocker.
	assert.equal(
		mod.advanceRefusal('detect', {
			source: 'folder',
			detection: nothingFound(),
			folderRows: [
				{
					id: 'row-1',
					path: '/Users/dj/Music',
					scan: {
						path: '/Users/dj/Music',
						exists: true,
						readable: true,
						denied: false,
						detail: 'readable',
						audio_files: 12,
						icloud_placeholders: 0,
						how_to_grant: '',
						sample: []
					}
				}
			],
			job: null
		}),
		null
	);
});

// ------------------------------------------------------------- the store

test('collapse minimises without closing', () => {
	mod.openSetupOverlay();
	assert.equal(mod.setupOverlay.open, true);
	mod.collapseSetupOverlay();
	assert.equal(mod.setupOverlay.open, true, 'a chip is still an open overlay');
	assert.equal(mod.setupOverlay.collapsed, true);
	mod.expandSetupOverlay();
	assert.equal(mod.setupOverlay.collapsed, false);
});

test('collapsing something that is not open does nothing', () => {
	mod.collapseSetupOverlay();
	assert.equal(mod.setupOverlay.open, false);
	assert.equal(mod.setupOverlay.collapsed, false);
});

test('re-opening always clears the chip and the incomplete note', () => {
	mod.closeSetupOverlay({ incomplete: true });
	assert.equal(mod.setupOverlay.incomplete, true);
	mod.openSetupOverlay();
	assert.equal(mod.setupOverlay.incomplete, false);
	assert.equal(mod.setupOverlay.collapsed, false);
});

test('dismissing without importing closes into an honest incomplete state', () => {
	mod.openSetupOverlay();
	mod.closeSetupOverlay({ incomplete: true });
	assert.equal(mod.setupOverlay.open, false);
	assert.equal(mod.setupOverlay.incomplete, true);
	assert.equal(mod.setupOverlay.holdEmptyReopen, true);
	// A finished setup closes silently instead, and still holds the auto-open.
	mod.openSetupOverlay();
	assert.equal(mod.setupOverlay.holdEmptyReopen, false);
	mod.closeSetupOverlay();
	assert.equal(mod.setupOverlay.incomplete, false);
	assert.equal(mod.setupOverlay.holdEmptyReopen, true);
});

test('the layout keeps the wizard mounted while the incomplete note is due', () => {
	// The note is drawn by SetupOverlay itself, in the state AFTER a close
	// (`open` false, `incomplete` true). The root layout mounts the lazily
	// loaded component behind a guard, so a guard on `open` alone unmounts the
	// note before it can render (Codex review of #3862, P2). Pinned by source
	// shape, as the markup facts above are: the guard names both flags.
	const layout = read('src/routes/+layout.svelte');
	assert.match(layout, /const setupMounted = \$derived\(setupOverlay\.open \|\| setupOverlay\.incomplete\);/);
	assert.match(layout, /shouldAutoOpenEmptyLibrarySetup\(/);
	assert.match(layout, /setupOverlay\.holdEmptyReopen/);
	assert.match(layout, /\{#if setupMounted\}\s*\{#await loadSetupOverlay\(\)\}/);
	assert.match(layout, /\{:then \{ default: SetupOverlay \}\}\s*<SetupOverlay \/>/);
	assert.doesNotMatch(layout, /\{#if setupOpen\}/);
});

test('the layout covers the app while the wizard chunk is in flight', () => {
	// `setupOpen` yields the boot gate the moment setup opens, so an {#await}
	// with no pending branch left the app usable for the length of the chunk
	// download on a cold cache (Codex review of #3862 at 079105b3, P2). The
	// pending branch draws the same backdrop the failure branch does, with a
	// status the e2e (lazy-chunk-failures.spec.ts) waits on.
	const layout = read('src/routes/+layout.svelte');
	const pending = layout.match(/\{#await loadSetupOverlay\(\)\}([^]*?)\{:then \{ default: SetupOverlay \}\}/);
	assert.ok(pending, 'the await has a pending branch');
	assert.match(pending[1], /class="setup-load-backdrop"/);
	assert.match(pending[1], /role="status" aria-label="Setup is loading"/);
});

test('the layout requests the wizard chunk only once setup mounts', () => {
	// The bundle budget's library surface is what first paint downloads, so
	// the chunk's import() must not run at script level, where it would start
	// the fetch on every boot before the mount guard was consulted (Codex
	// review of #3862 at 1fb0cc41, P1). The one import() sits inside the
	// memoizing loader the {#await} above calls, and nowhere else.
	const layout = read('src/routes/+layout.svelte');
	// The runtime import(), not the `typeof import(...)` type of its module.
	const runtimeImport = /(?<!typeof )import\('\$lib\/components\/setup\/SetupOverlay\.svelte'\)/g;
	assert.equal(layout.match(runtimeImport)?.length, 1, 'exactly one import() of the setup chunk');
	const loader = layout.match(/function loadSetupOverlay\(\)[^]*?\n\t\}\n/);
	assert.ok(loader, 'the memoizing loader exists');
	assert.match(loader[0], runtimeImport, 'the import() is inside the loader');
	assert.match(loader[0], /if \(setupOverlayModule === null\)/);
	assert.doesNotMatch(layout, /const setupOverlayModule = import\(/);
});

// ------------------------------------------------ final vs unfinished probe

test('an unfinished health probe is NOT a refusal', async () => {
	// This is the sentence the maintainer saw: grey, one tick after load, on a healthy
	// engine, with the buttons dead beneath it.
	const fresh = await loadTypeScriptModule('tests/unit/fixtures/setup-overlay-entry.ts', {
		viteApiBase: API_BASE
	});
	assert.equal(fresh.capabilities.flavor, 'unknown');
	assert.equal(fresh.finalSetupRefusal(), null, 'unknown is "not yet", never "no"');
	assert.equal(fresh.setupProbePending(), true);
	// setupRefusal still reports it, for callers that want the sentence.
	assert.match(fresh.setupRefusal(), /daemon not identified yet/);
});

test('a legacy daemon IS a final refusal', async () => {
	const fresh = await loadTypeScriptModule('tests/unit/fixtures/setup-overlay-entry.ts', {
		viteApiBase: API_BASE
	});
	const { contract_rev, engine_version, boot_id, ...legacy } = engineHealth();
	globalThis.fetch = async () => jsonResponse(legacy);
	assert.equal(await fresh.capabilities.probe(), 'legacy');
	assert.match(fresh.finalSetupRefusal(), /setup API not offered/);
	assert.equal(fresh.setupProbePending(), false);
});

// ---------------------------------------------------- the wizard's own state

test('detectState separates "never asked" from "asked and answered"', async () => {
	const fresh = await loadTypeScriptModule('tests/unit/fixtures/setup-overlay-entry.ts', {
		viteApiBase: API_BASE
	});
	assert.equal(fresh.setupWizard.detectState, 'idle');
	globalThis.fetch = async (request) => {
		const { pathname } = new URL(request.url);
		if (pathname === '/api/v1/health') return jsonResponse(engineHealth());
		return jsonResponse({
			library_empty: true,
			tracks: 0,
			playlists: 0,
			state_db: fileProbe('/data/state/state.db', false),
			data_dir: '/data',
			dismissed: false,
			dev_mode: false,
			should_show_wizard: true,
			stages: ['detect', 'snapshot', 'decrypt', 'ingest', 'analysis'],
			folder_stages: ['detect', 'scan', 'ingest'],
			last_import: null,
			rekordbox: nothingFound(),
			permissions: null
		});
	};
	await fresh.setupWizard.load();
	assert.equal(fresh.setupWizard.detectState, 'answered');
	assert.equal(fresh.setupWizard.detection.import_source, null);
});

test('a load that could not reach the daemon is FAILED, and retries', async () => {
	// The boot race: the overlay mounts in the same tick the layout probes,
	// and in the packaged app the engine may not be listening yet at all.
	// Nothing re-ran load(), so the step sat on a scanning state forever.
	const fresh = await loadTypeScriptModule('tests/unit/fixtures/setup-overlay-entry.ts', {
		viteApiBase: API_BASE
	});
	globalThis.fetch = async () => {
		throw new Error('connection refused');
	};
	await fresh.setupWizard.load();
	assert.equal(fresh.setupWizard.detectState, 'failed');
	assert.equal(fresh.setupWizard.detection, null);

	// The engine comes up. ensureLoaded must ASK AGAIN rather than inherit
	// the verdict of a probe that never got an answer.
	let asked = 0;
	globalThis.fetch = async (request) => {
		const { pathname } = new URL(request.url);
		asked += 1;
		if (pathname === '/api/v1/health') return jsonResponse(engineHealth());
		return jsonResponse({
			library_empty: true,
			tracks: 0,
			playlists: 0,
			state_db: fileProbe('/data/state/state.db', false),
			data_dir: '/data',
			dismissed: false,
			dev_mode: false,
			should_show_wizard: true,
			stages: [],
			folder_stages: [],
			last_import: null,
			rekordbox: detection(),
			permissions: null
		});
	};
	await fresh.setupWizard.ensureLoaded();
	assert.ok(asked >= 2, `expected a retry, saw ${asked} request(s)`);
	assert.equal(fresh.setupWizard.detectState, 'answered');
	assert.equal(fresh.setupWizard.detection.import_source, '/data/master.plain.db');

	// And it does NOT ask a third time once it has an answer.
	const before = asked;
	await fresh.setupWizard.ensureLoaded();
	assert.equal(asked, before, 'ensureLoaded re-asked after a successful load');
});

// -------------------------------------------------------------- the markup

test('the refusal renders inline, not only in a hover title', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	// The <p class="why"> block is the inline copy. Its presence next to the
	// disabled Continue is the fix for "no available next step".
	assert.match(overlay, /class="why"/);
	assert.match(overlay, /Continue is not available: \{nextRefusal\}/);
	assert.match(overlay, /Use one of the three options above instead/);
});

test('fatal sentences are alerts painted in the danger colour', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /class="fatal" role="alert"/);
	assert.match(overlay, /\.fatal,\s*\n\s*\.probes li\.danger \{\s*\n\s*color: var\(--danger\);/);
	// And the probe line that is the reason is red too, with its own alert.
	assert.match(overlay, /class:danger=\{row\.danger\}/);
});

test('the three escape actions are rendered ungated', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	// Look again and Choose a folder carry no `disabled` at all; Continue
	// without importing is gated only on `busy`, never on a blocker.
	assert.match(overlay, /onclick=\{\(\) => setupWizard\.redetect\(\)\}/);
	assert.match(overlay, /onclick=\{\(\) => setupWizard\.useSource\('folder'\)\}/);
	assert.match(overlay, /onclick=\{\(\) => void dismissAndClose\(\)\}/);
	assert.doesNotMatch(overlay, /disabled=\{fatal/);
	assert.doesNotMatch(overlay, /disabled=\{blockers/);
});

test('STANDALONE-08: neither source radio is selected until the operator chooses', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /checked=\{source === 'rekordbox'\}/);
	assert.match(overlay, /checked=\{source === 'folder'\}/);
	assert.doesNotMatch(overlay, /checked=\{true\}/);
});

test('STANDALONE-08: welcome copy does not assume rekordbox import', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /when you choose to start that import/);
	assert.doesNotMatch(overlay, /Setting it up means\s+reading your existing rekordbox/);
});

test('STANDALONE-08: the neutral detect step offers dismissal and refuses Continue', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /\{#if step === 'detect' && source === null\}/);
	assert.match(overlay, /Choose an import source above/);
	assert.match(overlay, /Continue is not available: \{nextRefusal\}/);
});

test('the wizard gates on the FINAL refusal, never on an unfinished probe', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /const refusal = \$derived\(finalSetupRefusal\(\)\)/);
	// setupRefusal() folds "not yet" into "no"; a surface must not read it.
	assert.doesNotMatch(overlay, /\$derived\(setupRefusal\(\)\)/);
});

// REQ: SETUP-10
test('the folder step offers a native picker beside the path field', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /class="folder-path-row"/);
	assert.match(overlay, /type="button"\s*\n\s*class="folder-pick"/);
	assert.match(overlay, /aria-label="Choose a folder"/);
	assert.match(overlay, /bind:value=\{row\.path\}/);
	assert.match(overlay, /aria-label="Folder to import"/);
});

test('the folder picker guards on a desktop shell and opens a directory dialog', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	// Either shell (Tauri or Electron) through the one bridge; a tab has none.
	assert.match(overlay, /return nativeShellKind\(\) !== null/);
	assert.match(overlay, /canUseNativeFolderPicker/);
	assert.match(overlay, /await pickFolder\('Choose a folder'\)/);
	const bridge = read('src/lib/shell/native-shell.ts');
	assert.match(bridge, /__TAURI_INTERNALS__/);
	assert.match(bridge, /await import\('@tauri-apps\/plugin-dialog'\)/);
	assert.match(bridge, /directory: true, multiple: false/);
	assert.match(bridge, /electron\.pickFolder\(\{ title \}\)/);
	assert.match(
		overlay,
		/if \(typeof selected === 'string'\) \{\s*\n\s*setupWizard\.folderRows = setupWizard\.folderRows\.map/
	);
});

test('the folder step can add and remove rows once a path is entered', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /setupWizard\.addFolderRow\(\)/);
	assert.match(overlay, /aria-label="Add another folder"/);
	assert.match(overlay, /setupWizard\.removeFolderRow\(row\.id\)/);
	assert.match(overlay, /aria-label="Remove folder"/);
	assert.match(overlay, /\{#if folderRows\.length > 1\}/);
});

// REQ: SETUP-09
test('the folder path placeholder is dim and italic, not the input itself', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /\.folder-form input::placeholder\s*\{/);
	assert.match(overlay, /font-style:\s*italic/);
	assert.match(overlay, /opacity:\s*0\.65/);
	assert.match(overlay, /color:\s*var\(--muted\)/);
	// Dimming belongs on the pseudo-element only; typed text keeps normal styles.
	const baseInputRule = overlay.match(/\.folder-form input\s*\{[^}]+\}/)?.[0] ?? '';
	assert.doesNotMatch(baseInputRule, /font-style:\s*italic/);
	assert.doesNotMatch(baseInputRule, /opacity:/);
	assert.doesNotMatch(baseInputRule, /color:\s*var\(--muted\)/);
});

// REQ: SETUP-13
test('the folder picker is a real control, and says so when it cannot run', () => {
	// A native picker shipped on main while this branch was open, replacing the
	// honestly-disabled placeholder this test used to guard. The requirement is
	// unchanged in spirit: there is a visible picker affordance, and it never
	// pretends to work where it cannot.
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /class="folder-pick"/);
	assert.match(overlay, /onclick=\{\(\) => void chooseFolder\(row\.id\)\}/);
	// outside the desktop shell there is no native picker, and the control says
	// which app can do it rather than failing silently
	assert.match(overlay, /!nativeFolderPicker/);
	assert.match(overlay, /available in the Open DJ desktop app/);
});

// REQ: SETUP-11
test('denied folder candidates render as refused chips, not hidden', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /\{#if candidate\.readable\}/);
	assert.match(overlay, /class="folder-chip refused"/);
	assert.match(overlay, /title=\{candidate\.detail\}/);
});

// ---------------------------------------------------------------- forward/back
//
// the maintainer, Wed 16 Sep 2026: "We also can't go forward or back in the first-run
// setup which is confusing - add forward and back. Block going forward if it's
// impossible to let user skip a section ofc."
//
// These read the source, like the rest of this file. The BEHAVIOUR of the
// rules they depend on is proven against the real module in
// setup-wizard-navigation.test.mjs; what is checked here is that the markup
// actually wires the controls to those rules.

// REQ: SETUP-12
test('every step panel renders a Back control', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	// one shared snippet, rendered once per panel (detect has two branches),
	// so Back cannot be present on some steps and missing on others.
	assert.match(overlay, /\{#snippet backButton\(\)\}/);
	const renders = overlay.match(/\{@render backButton\(\)\}/g) ?? [];
	assert.equal(renders.length, 8, `expected a Back control on all 8 panels, found ${renders.length}`);
});

test('Back is gated by backRefusal, and says why in the same breath', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /const backWhy = \$derived\(backRefusal\(/);
	const snippet = overlay.match(/\{#snippet backButton\(\)\}[\s\S]*?\{\/snippet\}/)?.[0] ?? '';
	assert.match(snippet, /disabled=\{backWhy !== null/, 'Back must be disabled by the refusal');
	assert.match(snippet, /title=\{backWhy \?\?/, 'the refusal must be the tooltip, not a separate string');
});

test('Back hands the live job to the rule instead of guessing', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	// The job row lives in jobsStore; the wizard store keeps no copy. If this
	// call drops the argument, a running import stops blocking Back.
	assert.match(overlay, /setupWizard\.back\(job\)/);
});

test('the breadcrumb walks this branch only, and past steps are real links', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /\{#each route as name, index \(name\)\}/);
	assert.match(overlay, /const route = \$derived\(visibleSteps\(source\)\)/);
	assert.match(overlay, /class="step-link"/);
	assert.match(overlay, /onclick=\{\(\) => setupWizard\.goTo\(name\)\}/);
	// and it no longer hard-codes the full step list, which would show the
	// folder branch a confirm step it never visits
	assert.doesNotMatch(overlay, /\{#each WIZARD_STEPS as name/);
});

test('the user is told where they are, not left to count pills', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /Step \{position\} of \{total\}/);
});
