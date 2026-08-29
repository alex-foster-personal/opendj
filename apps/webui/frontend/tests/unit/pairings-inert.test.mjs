/**
 * The pairings capture surface is RETIRED, not pending.
 *
 * `/api/v1/pairings/sync-snapshots` and `/api/v1/pairings/alignments` were
 * phantoms: the frontend called both, no daemon ever published either, and
 * every call 404'd on the legacy and rebuilt engines alike. A router and repo
 * for them exist on two archive branches (718cc812, 6f39ab99) but were never
 * registered in app.py, and their recorded verdict is "rewrite after the
 * durable pairing gate", not "restore". So the UI now says so instead of
 * asking.
 *
 * ON PROVING "no request fired": CreatePairingSheet.svelte cannot be mounted
 * here (node:test + esbuild, no component mount infra - same constraint
 * capability-gating-markup.test.mjs documents). A mounted assertion would
 * anyway be the weaker claim. What is asserted instead is stronger: the
 * component holds no fetch call site at all, and no file in src/ names either
 * path outside a comment. A call that does not exist cannot fire. The one
 * execution-grade check that IS available - that the deleted module is gone
 * from the build graph - runs against an armed fetch seam that records and
 * refuses every request.
 *
 * Regression lines:
 * - if any Create-pairing action loses `disabled` or its PARITY-TODO title then
 *   a dead control looks live again
 * - if a fetch reappears in CreatePairingSheet.svelte then the sheet is back to
 *   404ing against a route nobody serves
 * - if pairing-alignments.svelte.ts is restored without its server routes then
 *   the phantom is back
 * - if any src/ module starts requesting either pairings path then the retire
 *   decision has been silently reversed
 * - if the deck picker goes inert too then real engine state has been thrown
 *   away along with the phantoms
 */
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const SRC = join(FRONTEND, 'src');
const SHEET = join(SRC, 'lib/components/rb/CreatePairingSheet.svelte');
const RETIRED_MODULE = join(SRC, 'lib/rb/pairing-alignments.svelte.ts');

const INERT_TITLE = 'not implemented - see PARITY-TODO';
const PHANTOM_PATHS = ['/api/v1/pairings/sync-snapshots', '/api/v1/pairings/alignments'];
/** The three sheet actions that needed the routes that never shipped. */
const RETIRED_ACTIONS = ['Align hotcues', 'Reload sync', 'Capture'];

const sheet = readFileSync(SHEET, 'utf8');

function sourceFiles(dir) {
	const out = [];
	for (const entry of readdirSync(dir)) {
		const path = join(dir, entry);
		if (statSync(path).isDirectory()) out.push(...sourceFiles(path));
		else if (/\.(svelte|ts)$/.test(entry)) out.push(path);
	}
	return out;
}

/** Strip line comments and block comments so a prose mention of a dead path is
 * not read as a call site. */
function stripComments(source) {
	return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');
}

// ------------------------------------------------------- the inert controls

test('all three Create-pairing actions are disabled and say why', () => {
	const footer = sheet.slice(sheet.indexOf('<footer>'), sheet.indexOf('</footer>'));
	for (const label of RETIRED_ACTIONS) {
		assert.ok(footer.includes(label), `${label} button is missing from the sheet`);
	}
	const buttons = footer.match(/<button[\s\S]*?<\/button>/g) ?? [];
	assert.equal(buttons.length, RETIRED_ACTIONS.length, 'the footer holds exactly the three actions');
	for (const button of buttons) {
		assert.match(button, /\bdisabled\b/, `a footer action lost disabled: ${button}`);
		assert.match(button, /title=\{INERT_TITLE\}/, `a footer action lost its tooltip: ${button}`);
	}
	assert.ok(
		sheet.includes(`const INERT_TITLE = '${INERT_TITLE}';`),
		'the tooltip literal must stay exactly the shared PARITY-TODO wording'
	);
});

test('no action can fire a request, because the sheet holds no call site', () => {
	const code = stripComments(sheet);
	assert.equal(
		/(^|[^A-Za-z0-9_])fetch\(/.test(code),
		false,
		'CreatePairingSheet must not fetch - its routes do not exist'
	);
	for (const path of PHANTOM_PATHS) {
		assert.equal(code.includes(path), false, `${path} is still requested from the sheet`);
	}
	// A disabled button with no handler is the whole point: no onclick, nothing
	// to schedule, nothing to await.
	const footer = code.slice(code.indexOf('<footer>'), code.indexOf('</footer>'));
	assert.equal(/onclick/.test(footer), false, 'an inert action must not carry a handler');
});

test('the deck picker stays live - real engine state is not collateral damage', () => {
	assert.match(sheet, /deckStates\[d\]\.stable_id !== null/);
	assert.match(sheet, /onchange=\{\(\) => _toggle\(d\.id\)\}/);
	assert.equal(
		/<input[^>]*type="checkbox"[^>]*disabled/.test(sheet),
		false,
		'the picker reads real deck state and must not be disabled'
	);
});

// ------------------------------------------------------ the deleted module

test('pairing-alignments.svelte.ts is gone from disk and from the build graph', async () => {
	assert.equal(existsSync(RETIRED_MODULE), false, 'the retired module is back on disk');

	const requested = [];
	const originalFetch = globalThis.fetch;
	globalThis.fetch = async (request) => {
		requested.push(typeof request === 'string' ? request : request.url);
		throw new Error(`no request was expected, got ${requested.at(-1)}`);
	};
	try {
		await assert.rejects(
			() => loadTypeScriptModule('src/lib/rb/pairing-alignments.svelte.ts'),
			'the retired module must not resolve'
		);
	} finally {
		globalThis.fetch = originalFetch;
	}
	assert.deepEqual(requested, [], 'nothing may be requested while the module is absent');
});

// ---------------------------------------------------------- the tree guard

test('no module in src/ requests either retired pairings path', () => {
	const offenders = [];
	for (const file of sourceFiles(SRC)) {
		const code = stripComments(readFileSync(file, 'utf8'));
		for (const path of PHANTOM_PATHS) {
			if (code.includes(path)) offenders.push(`${file.slice(SRC.length + 1)} -> ${path}`);
		}
	}
	assert.deepEqual(offenders, [], `retired pairings paths are back in the tree:\n${offenders.join('\n')}`);
});

test('nothing imports the retired module', () => {
	const offenders = sourceFiles(SRC)
		.filter((file) => readFileSync(file, 'utf8').includes('pairing-alignments'))
		.map((file) => file.slice(SRC.length + 1));
	assert.deepEqual(offenders, [], 'a live import of the retired alignments module is back');
});
