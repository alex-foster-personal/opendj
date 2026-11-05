// PGCL-7693b: stepping the pin days slider must not rerun the Settings opening
// work. The opening effect seeds the number rows untracked, so a value change
// does not refocus the search box (which would steal the slider's arrow keys)
// or clear the AI search state.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const src = readFileSync(
	new URL('../../src/lib/components/settings/SettingsOverlay.svelte', import.meta.url),
	'utf8'
);

function openingEffect() {
	const m = src.match(
		/\$effect\(\(\) => \{\n\t\tif \(!settingsOverlay\.open\) \{[\s\S]*?searchEl\?\.focus\(\)\);\n\t\}\);/
	);
	assert.ok(m, 'the opening effect must exist');
	return m[0];
}

test('the opening effect seeds the number rows untracked', () => {
	const body = openingEffect();
	assert.match(body, /numberDraft = untrack\(_seedNumbers\);/);
	assert.doesNotMatch(body, /numberDraft = _seedNumbers\(\);/);
	assert.match(src, /import \{[^}]*\buntrack\b[^}]*\} from 'svelte';/);
});

test('the pin days effect still re-seeds the rows on a value change (control)', () => {
	assert.match(
		src,
		/\$effect\(\(\) => \{\n\t\tvoid uiPrefs\.pin_visible_days;\n\t\tnumberDraft = _seedNumbers\(\);\n\t\}\);/
	);
});
