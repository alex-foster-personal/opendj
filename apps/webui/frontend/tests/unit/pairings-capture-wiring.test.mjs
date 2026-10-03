/**
 * PAIR-03 wired the Create-pairing sheet's Align hotcues and Reload sync
 * actions onto the durable capture API that PAIR-01 (#1208) and PAIR-02
 * (#1209, PR #1398) already landed. This file replaces pairings-inert.test.mjs,
 * which encoded the deliberately-dead state before the routes existed:
 * `/api/v1/pairings/sync-snapshots` and `/api/v1/pairings/alignments` were
 * phantoms then, so the old file's job was proving nothing called them. They
 * are real now, so the job here is different -- proving the two actions call
 * them through exactly one place (lib/api.ts, the one typed client) and
 * nowhere else, and that the sheet's buttons carry live handlers rather than
 * the retired `disabled` / `title='not implemented - see PARITY-TODO'` markup.
 * Capture is untouched by PAIR-03: it already posts to the older, unrelated
 * generic `/api/v1/pairings` entity route (lib/api.ts's createPairing).
 *
 * ON PROVING single-client discipline: CreatePairingSheet.svelte cannot be
 * mounted here (node:test + esbuild, no component mount infra - same
 * constraint the file this replaces documented). What is asserted instead:
 * a POSITIVE control that lib/api.ts does name both capture paths (so the
 * absence check below is proven able to find something before it is trusted
 * to find nothing elsewhere - see .claude/rules/verification.md), that no
 * OTHER hand-written src/ file names either path, and that the sheet itself
 * holds no fetch call site of its own.
 *
 * Regression lines:
 * - if either capture path appears in a hand-written src/ file other than
 *   lib/api.ts then a second HTTP client has been invented for it
 * - if CreatePairingSheet.svelte grows a raw fetch() then a route bypassed
 *   the typed client
 * - if Align hotcues or Reload sync loses its onclick then the action went
 *   back to disabled-and-inert without anyone deciding that on purpose
 * - if pairing-alignments.svelte.ts (the pre-retirement module name) comes
 *   back, it duplicates lib/rb/pairing-capture.ts's job under the old name
 * - if the deck picker goes inert too then real engine state has been thrown
 *   away along with the wiring
 */
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const SRC = join(FRONTEND, 'src');
const SHEET = join(SRC, 'lib/components/rb/CreatePairingSheet.svelte');
const API_MODULE = join(SRC, 'lib/api.ts');
const RETIRED_MODULE = join(SRC, 'lib/rb/pairing-alignments.svelte.ts');

const CAPTURE_PATHS = ['/api/v1/pairings/sync-snapshots', '/api/v1/pairings/alignments'];

const sheet = readFileSync(SHEET, 'utf8');

/** Generated straight from the live OpenAPI schema on every backend route
 * change (`pnpm run api:gen`); it carries only type declarations, never a
 * call site, so it is exempt from the hand-written-call-site guard below. */
const GENERATED_FILES = [join(SRC, 'lib/api-types.ts')];

function sourceFiles(dir) {
	const out = [];
	for (const entry of readdirSync(dir)) {
		const path = join(dir, entry);
		if (statSync(path).isDirectory()) out.push(...sourceFiles(path));
		else if (/\.(svelte|ts)$/.test(entry) && !GENERATED_FILES.includes(path)) out.push(path);
	}
	return out;
}

/** Strip line comments and block comments so a prose mention of a path is not
 * read as a call site. */
function stripComments(source) {
	return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');
}

// --------------------------------------------------- single-client discipline

test('both capture paths are named in lib/api.ts (positive control)', () => {
	const api = stripComments(readFileSync(API_MODULE, 'utf8'));
	for (const path of CAPTURE_PATHS) {
		assert.ok(
			api.includes(path),
			`lib/api.ts must call ${path} - the absence check below is only trustworthy once this fires`
		);
	}
});

test('no hand-written src/ file other than lib/api.ts requests either capture path', () => {
	const offenders = [];
	for (const file of sourceFiles(SRC)) {
		if (file === API_MODULE) continue;
		const code = stripComments(readFileSync(file, 'utf8'));
		for (const path of CAPTURE_PATHS) {
			if (code.includes(path)) offenders.push(`${file.slice(SRC.length + 1)} -> ${path}`);
		}
	}
	assert.deepEqual(offenders, [], `a second client for the capture routes:\n${offenders.join('\n')}`);
});

test('the sheet holds no fetch call site of its own', () => {
	assert.equal(
		/(^|[^A-Za-z0-9_])fetch\(/.test(stripComments(sheet)),
		false,
		'CreatePairingSheet must call through lib/rb/pairing-capture.ts, not fetch directly'
	);
});

// -------------------------------------------------------------- live wiring

test('Align hotcues and Reload sync carry live handlers, not the retired inert markup', () => {
	const footer = sheet.slice(sheet.indexOf('<footer>'), sheet.indexOf('</footer>'));
	const buttons = footer.match(/<button[\s\S]*?<\/button>/g) ?? [];
	assert.equal(buttons.length, 3, 'the footer holds Align hotcues, Reload sync and Capture');
	assert.match(buttons[0], /Align hotcues/);
	assert.match(buttons[0], /onclick=\{\(\) => void _alignHotcues\(\)\}/);
	assert.match(buttons[1], /Reload sync/);
	assert.match(buttons[1], /onclick=\{\(\) => void _reloadSync\(\)\}/);
	assert.match(buttons[2], /onclick=\{\(\) => void _save\(\)\}/, 'Capture must still save the frozen pairing snapshot');
	for (const button of buttons) {
		assert.doesNotMatch(
			button,
			/rb-inert|not implemented - see PARITY-TODO/,
			`an action still carries the retired inert markup: ${button}`
		);
	}
});

test('the sheet calls the capture wiring from lib/rb/pairing-capture, not a bespoke client', () => {
	assert.match(sheet, /from '\$lib\/rb\/pairing-capture'/);
	assert.match(sheet, /\balignHotcues\b/);
	assert.match(sheet, /\blatestSyncSnapshot\b/);
});

test('the deck picker renders only the dispatcher snapshot', () => {
	assert.match(sheet, /snapshot\?\.decks \?\? \[\]/);
	assert.match(sheet, /onchange=\{\(\) => _toggle\(d\.deck_id\)\}/);
	assert.equal(
		/<input[^>]*type="checkbox"[^>]*disabled/.test(sheet),
		false,
		'the picker reads real deck state and must not be disabled'
	);
});

// ------------------------------------------------------ the deleted module

test('pairing-alignments.svelte.ts (the pre-retirement module name) stays gone', () => {
	assert.equal(existsSync(RETIRED_MODULE), false, 'the retired module is back on disk under its old name');
});

test('nothing imports the retired module', () => {
	const offenders = sourceFiles(SRC)
		.filter((file) => readFileSync(file, 'utf8').includes('pairing-alignments'))
		.map((file) => file.slice(SRC.length + 1));
	assert.deepEqual(offenders, [], 'a live import of the retired alignments module is back');
});
