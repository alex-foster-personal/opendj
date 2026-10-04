/**
 * BrowserPanel ingest-coverage health-dot contract.
 *
 * The node:test harness cannot mount a Svelte component, so this evaluates
 * the real browser-health-probes production module with its API dependencies.
 * It exercises the response values received from the existing typed API, not
 * a substitute API or DOM.
 *
 * [if] corrupt count is a nonnegative integer [then ⛔️] the dot must use it
 * as the genuine error signal rather than treat it as malformed.
 * [if] corrupt count is absent, negative, fractional, or nonnumeric [then ⛔️]
 * the dot must throw so _loadIngestCoverage renders an API-contract error.
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { build } from 'esbuild';
import { importBundledSource } from './import-bundled-source.mjs';

const LIB = fileURLToPath(new URL('../../src/lib', import.meta.url));
// Bundle the actual production module and its real API dependencies. No alias
// substitutes, rune shims, component stubs, or source-text function extraction.
const result = await build({
 entryPoints: [fileURLToPath(new URL('../../src/lib/rb/browser-health-probes.ts', import.meta.url))],
 alias: { $lib: LIB }, bundle: true, format: 'esm', platform: 'node',
 define: { 'import.meta.env.VITE_API_BASE': 'undefined', 'import.meta.env.DEV': 'false' },
 write: false
});
const { _coverageDot: dot } = await importBundledSource(result.outputFiles[0].text, 'browser-health-probes');

function coverage(corrupt, missing = 0) {
	return {
		on_disk: 3,
		unreachable: 0,
		missing: { lyrics: missing },
		corrupt: { lyrics: corrupt }
	};
}

test('valid coverage stays complete while a corrupt cache entry is a visible error', () => {
		assert.deepEqual(dot('Lyrics completion', coverage(0), 'lyrics'), {
		label: 'Lyrics completion',
		state: 'complete',
		detail: '3/3 playable complete, 0 missing, 0 broken links'
	});
	assert.deepEqual(dot('Lyrics completion', coverage(1, 1), 'lyrics'), {
		label: 'Lyrics completion',
		state: 'error',
		detail: '1 corrupt entry - 2/3 playable complete, 1 missing, 0 broken links'
	});
});

for (const [name, corrupt] of [
	['missing', undefined],
	['negative', -1],
	['fractional', 0.5],
	['nonnumeric', '1']
]) {
	test(`${name} corruption count fails the ingest coverage contract`, () => {
		assert.throws(
			() => dot('Lyrics completion', coverage(corrupt), 'lyrics'),
			/lyrics coverage corrupt count must be a nonnegative integer/
		);
	});
}
