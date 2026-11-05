// PGCL-7693b: a whole-number setting (pin days, step 1) reads "30 days", not
// "30.00 days"; a fractional one (wheel sensitivity, step 0.05) keeps two decimals.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const src = readFileSync(
	new URL('../../src/lib/components/settings/SettingsOverlay.svelte', import.meta.url),
	'utf8'
);

function extractFormatter() {
	const m = src.match(
		/function formatNumber\(value: number, step: number\): string \{\n([\s\S]*?)\n\t\}/
	);
	assert.ok(m, 'formatNumber(value, step) must exist in SettingsOverlay.svelte');
	return new Function('value', 'step', m[1]);
}

test('whole-number steps read as integers', () => {
	const formatNumber = extractFormatter();
	assert.equal(formatNumber(0, 1), '0');
	assert.equal(formatNumber(30, 1), '30');
});

test('fractional steps keep two decimals (control)', () => {
	const formatNumber = extractFormatter();
	assert.equal(formatNumber(1, 0.05), '1.00');
	assert.equal(formatNumber(1.25, 0.05), '1.25');
});

test('both the readout and the Reset title pass the step', () => {
	assert.match(
		src,
		/\{formatNumber\(numberValue\(def\), def\.control\.step\)\}\{def\.control\.unit\}/
	);
	assert.match(
		src,
		/\$\{formatNumber\(def\.control\.defaultValue, def\.control\.step\)\}\$\{def\.control\.unit\}/
	);
});
