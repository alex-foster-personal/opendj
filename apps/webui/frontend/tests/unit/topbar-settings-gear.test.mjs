// The Settings button draws the standard cog, not the old 8-square-tooth glyph
// that read as a sun at 12px (the maintainer, Mon 5 Oct 2026: "settings icon should be a
// gear - industry standard icon for it").
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const src = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
	'utf8'
);
const settingsButton = /<button[^>]*aria-label="Open settings"[\s\S]*?<\/button>/.exec(src)?.[0];

test('settings button draws the standard cog outline', () => {
	assert.ok(settingsButton, 'control: the Open settings button is found');
	assert.match(settingsButton, /<path d=\{SETTINGS_GEAR_PATH\} \/>/);
	assert.match(src, /const SETTINGS_GEAR_PATH =\s*'M12\.22 2h-\.44/);
	assert.match(settingsButton, /viewBox="0 0 24 24"/);
});

test('the sun-like square-tooth glyph is gone', () => {
	assert.doesNotMatch(settingsButton, /<rect\b/);
	assert.doesNotMatch(src, /GEAR_TOOTH_ANGLES/);
});

test('the gear snippet is attributed in NOTICE', () => {
	const notice = readFileSync(fileURLToPath(new URL('../../../../../NOTICE', import.meta.url)), 'utf8');
	assert.match(notice, /Lucide icons[\s\S]{0,200}TopBar\.svelte[\s\S]{0,200}ISC/);
});
