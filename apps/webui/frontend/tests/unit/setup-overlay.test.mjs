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
 * - if a null detection ever reports phase 'answered' then the not-found
 *   visual can be painted before an answer exists, which is the whole bug
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

test('a null detection is SCANNING, never a verdict', () => {
	// The bug: null meant "never asked", "asking now" and "asked and failed"
	// all at once, and the step painted all three as one grey sentence that
	// nothing would ever clear.
	assert.equal(mod.detectPhase(null, false), 'scanning');
	assert.equal(mod.detectPhase(null, true), 'scanning');
});

test('a re-ask is scanning too, even with a previous answer on screen', () => {
	assert.equal(mod.detectPhase(detection(), true), 'scanning');
	assert.equal(mod.detectPhase(detection(), false), 'answered');
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
		folderScan: null,
		job: null
	});
	assert.match(refusal, /rekordbox_not_found/);
	// The folder branch must NOT inherit a rekordbox blocker.
	assert.equal(
		mod.advanceRefusal('detect', {
			source: 'folder',
			detection: nothingFound(),
			folderScan: {
				path: '/Users/dj/Music',
				exists: true,
				readable: true,
				denied: false,
				detail: 'readable',
				audio_files: 12,
				icloud_placeholders: 0,
				how_to_grant: '',
				sample: []
			},
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
	// A finished setup closes silently instead.
	mod.openSetupOverlay();
	mod.closeSetupOverlay();
	assert.equal(mod.setupOverlay.incomplete, false);
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

test('the wizard gates on the FINAL refusal, never on an unfinished probe', () => {
	const overlay = read('src/lib/components/setup/SetupOverlay.svelte');
	assert.match(overlay, /const refusal = \$derived\(finalSetupRefusal\(\)\)/);
	// setupRefusal() folds "not yet" into "no"; a surface must not read it.
	assert.doesNotMatch(overlay, /\$derived\(setupRefusal\(\)\)/);
});

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
