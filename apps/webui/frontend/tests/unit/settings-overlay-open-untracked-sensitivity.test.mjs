// requirement: MIXGRP-04
// Codex P2 on PR #6829: stepping a drag-sensitivity slider must not rerun the
// Settings opening work. The opening effect seeds the number rows untracked,
// so a slider change does not clear the AI state or refocus the search box
// (which would steal the slider's arrow keys).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const src = readFileSync(
	new URL('../../src/lib/components/settings/SettingsOverlay.svelte', import.meta.url),
	'utf8'
);

function openingEffect() {
	const m = src.match(/\$effect\(\(\) => \{\n\t\tif \(!settingsOverlay\.open\) \{[\s\S]*?searchEl\?\.focus\(\)\);\n\t\}\);/);
	assert.ok(m, 'the opening effect must exist');
	return m[0];
}

test('the opening effect seeds the number rows untracked', () => {
	const body = openingEffect();
	assert.match(body, /numberDraft = untrack\(_seedNumbers\);/);
	assert.doesNotMatch(body, /numberDraft = _seedNumbers\(\);/);
	assert.match(src, /import \{[^}]*\buntrack\b[^}]*\} from 'svelte';/);
});

test('the sensitivity subscription still re-seeds the rows on a change (control)', () => {
	assert.match(
		src,
		/return subscribeWheelSensitivity\(\(\) => \{\n\t\t\tnumberDraft = _seedNumbers\(\);\n\t\t\}\);/
	);
});

test('a drag-sensitivity change re-seeds the rows from its own tracked effect (control)', () => {
	// The opening effect reads _seedNumbers untracked, so this separate effect
	// is what keeps the drag rows live; deleting it must turn this test red.
	assert.match(
		src,
		/\$effect\(\(\) => \{\n\t\tvoid dragSensitivity\.base;\n\t\tvoid dragSensitivity\.falloffPx;\n\t\tnumberDraft = _seedNumbers\(\);\n\t\}\);/
	);
	assert.doesNotMatch(openingEffect(), /dragSensitivity\./, 'the opening effect must not track drag sensitivity');
});
