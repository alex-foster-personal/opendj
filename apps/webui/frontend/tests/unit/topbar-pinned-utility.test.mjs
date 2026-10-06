// Settings and the theme/skin toggle never collapse in a narrow window; only
// the inert info icon is evictable (the maintainer, Mon 5 Oct 2026: both buttons were
// missing from a 1508px window because the max-width 1530px tier hid them).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const src = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
	'utf8'
);
// Comments stripped: the rationale comment names the pinned class in the media block.
const style = src.slice(src.indexOf('<style')).replace(/\/\*[\s\S]*?\*\//g, '');

test('settings button is pinned, not in the evictable utility tier', () => {
	const m = /<button\s[^>]*class="([^"]*)"[^>]*aria-label="Open settings"/s.exec(src);
	assert.ok(m, 'control: the Open settings button is found');
	assert.match(m[1], /topbar-slot-pinned/);
	assert.doesNotMatch(m[1], /topbar-slot-utility/);
});

test('theme toggle is pinned, not in the evictable utility tier', () => {
	const m = /<button[^>]*class="(tb-icon theme-toggle[^"]*)"[^>]*onclick=\{toggleTheme\}/s.exec(src) ?? /<button\s+type="button"\s+class="(tb-icon theme-toggle[^"]*)"/s.exec(src);
	assert.ok(m, 'control: the theme toggle button is found');
	assert.match(m[1], /topbar-slot-pinned/);
});

test('no responsive rule hides the pinned slot', () => {
	assert.doesNotMatch(style, /\.topbar-slot-pinned[^{]*\{[^}]*display:\s*none/);
	assert.match(style, /\.rb-topbar \.topbar-slot-utility,/, 'control: the info icon stays evictable');
});
