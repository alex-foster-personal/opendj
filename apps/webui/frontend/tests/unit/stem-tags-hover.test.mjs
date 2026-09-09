import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const STEM_TAGS = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/StemTags.svelte', import.meta.url)
);
const source = readFileSync(STEM_TAGS, 'utf8').replaceAll('\r\n', '\n');

test('ready and unavailable STEMS tags use the shared rich hover and focus explainer', () => {
	assert.match(source, /import ControlExplainer from '\.\.\/deck\/ControlExplainer\.svelte'/);
	assert.equal(
		(source.match(/<ControlExplainer\b/g) ?? []).length,
		2,
		'both ready and unavailable render branches must be wrapped'
	);
	for (const status of ['none', 'invalid', 'ready']) {
		assert.match(source, new RegExp(`s\\.status === '${status}'`), `${status} state lacks explainer data`);
	}
	assert.equal(
		(source.match(/tabindex="0"/g) ?? []).length,
		2,
		'each explainer trigger must open on keyboard focus'
	);
	assert.equal(
		(source.match(/aria-label=\{_title\(/g) ?? []).length,
		2,
		'the native accessible tag label must remain on both inner tag rows'
	);
});

test('STEMS explainer exposes factual model, preset, format, total, and V/I/D details', () => {
	for (const detail of ['Status:', 'Model:', 'Preset:', 'Format:', 'Total:', 'GROUPS.map', '_fmtBytes']) {
		assert.ok(source.includes(detail), `STEMS explainer omits ${detail}`);
	}
});

// Regression: if hovering or focusing STEMS can still produce no explicit
// explanation then the pin is broken.
