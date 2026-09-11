/**
 * Vocal-analysis trigger: source pins for the network call
 * VocalAnalyzeButton.svelte makes on click (PARITY-08 / issue #1038), plus
 * pure refusal behavior. The mounted FastAPI production-fixture test in
 * tests/webui/test_vocals_routes.py drives the actual HTTP contract; this
 * suite does not simulate that API.
 *
 * Regression lines:
 *  - if analyzeVocalsFromStems stops sending mode "from-stems" then the
 *    button would silently request real demucs instead of the CPU path
 *  - if it stops sending exactly the clicked stable_id then a click on one
 *    row could analyze another
 *  - if VocalAnalyzeButton stops calling analyzeVocalsFromStems then the
 *    button and the production HTTP contract drift apart
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://vocals.example.test';
const SRC = fileURLToPath(new URL('../../src', import.meta.url));

let mod;
before(async () => {
	mod = await loadTypeScriptModule('tests/unit/fixtures/vocals-analyze-entry.ts', {
		viteApiBase: API_BASE
	});
});

test('analyzeVocalsFromStems is pinned to the mounted API request contract', () => {
	const source = readFileSync(`${SRC}/lib/rb/api-rb.ts`, 'utf8');
	assert.match(source, /fetch\(`\$\{RB_API_BASE\}\/api\/v1\/vocals\/analyze`, \{/);
	assert.match(source, /method: 'POST'/);
	assert.match(source, /body: JSON\.stringify\(\{ stable_ids: \[stable_id\], mode: 'from-stems' \}\)/);
});

test('VocalAnalyzeButton calls analyzeVocalsFromStems, not a fetch of its own', () => {
	const source = readFileSync(
		`${SRC}/lib/components/rb/browser/VocalAnalyzeButton.svelte`,
		'utf8'
	);
	assert.match(source, /import \{ analyzeVocalsFromStems,/);
	assert.match(source, /analyzeVocalsFromStems\(stableId\)/);
	assert.doesNotMatch(source, /fetch\(/);
});

test('VocalAnalyzeButton gates disabled/tooltip on vocalsAnalyzeRefusal, not its own logic', () => {
	const source = readFileSync(
		`${SRC}/lib/components/rb/browser/VocalAnalyzeButton.svelte`,
		'utf8'
	);
	assert.match(source, /import \{ vocalsAnalyzeRefusal \} from '\$lib\/rb\/vocals-analyze-refusal'/);
	assert.match(source, /vocalsAnalyzeRefusal\(stems\)/);
	assert.match(source, /disabled = \$derived\(refusal !== null \|\| busy\)/);
});

// ------------------------------------------------- vocalsAnalyzeRefusal

test('no stem bundle refuses, and says why', () => {
	assert.match(mod.vocalsAnalyzeRefusal(null), /needs a stem bundle/);
	assert.match(mod.vocalsAnalyzeRefusal({ status: 'none' }), /needs a stem bundle/);
});

test('an invalid stem bundle refuses too', () => {
	assert.match(
		mod.vocalsAnalyzeRefusal({ status: 'invalid', error: 'corrupt manifest' }),
		/needs a stem bundle/
	);
});

test('a ready stem bundle is not refused', () => {
	assert.equal(
		mod.vocalsAnalyzeRefusal({
			status: 'ready',
			model: 'htdemucs',
			preset: null,
			overlap: null,
			shifts: null,
			format: 'flac',
			total_bytes: 100,
			groups: {}
		}),
		null
	);
});

test('TrackTable wires the button into the stems column with the row stable_id', () => {
	const source = readFileSync(`${SRC}/lib/components/rb/browser/TrackTable.svelte`, 'utf8');
	assert.match(source, /import VocalAnalyzeButton from '\.\/VocalAnalyzeButton\.svelte'/);
	assert.match(
		source,
		/<VocalAnalyzeButton stableId=\{row\.stable_id\} stems=\{row\.stems\} \/>/
	);
});
