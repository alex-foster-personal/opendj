/**
 * BrowserPanel ingest-coverage health-dot contract.
 *
 * The node:test harness cannot mount a Svelte component, so this evaluates
 * BrowserPanel's real _coverageDot implementation directly from its source.
 * It exercises the response values received from the existing typed API, not
 * a substitute API or DOM.
 *
 * [if] corrupt count is a nonnegative integer [then ⛔️] the dot must use it
 * as the genuine error signal rather than treat it as malformed.
 * [if] corrupt count is absent, negative, fractional, or nonnumeric [then ⛔️]
 * the dot must throw so _loadIngestCoverage renders an API-contract error.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);

function coverageDot() {
	const source = readFileSync(PANEL, 'utf8');
	const start = source.indexOf('\tfunction _coverageDot(');
	const end = source.indexOf('\n\tasync function _loadIngestCoverage()', start);
	assert.ok(start >= 0 && end > start, 'could not isolate BrowserPanel._coverageDot');
	const functionSource = source
		.slice(start, end)
		.replace("label: LibraryHealthDot['label']", 'label')
		.replace('coverage: IngestCoverage', 'coverage')
		.replace("step: 'vocals' | 'stems' | 'lyrics'", 'step')
		.replace('): LibraryHealthDot {', ') {');
	return Function(`${functionSource}\nreturn _coverageDot;`)();
}

function coverage(corrupt, missing = 0) {
	return {
		on_disk: 3,
		unreachable: 0,
		missing: { lyrics: missing },
		corrupt: { lyrics: corrupt }
	};
}

test('valid coverage stays complete while a corrupt cache entry is a visible error', () => {
	const dot = coverageDot();
	assert.deepEqual(dot('Lyrics completion', coverage(0), 'lyrics'), {
		label: 'Lyrics completion',
		state: 'complete',
		detail: '3/3 complete, 0 missing, 0 unreachable'
	});
	assert.deepEqual(dot('Lyrics completion', coverage(1, 1), 'lyrics'), {
		label: 'Lyrics completion',
		state: 'error',
		detail: '1 corrupt entry - 2/3 complete, 1 missing, 0 unreachable'
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
			() => coverageDot()('Lyrics completion', coverage(corrupt), 'lyrics'),
			/lyrics coverage corrupt count must be a nonnegative integer/
		);
	});
}
