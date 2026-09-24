/**
 * FB-16 criterion 7: .bauble-label sign-in pill must not regress (pin 402509a74aa2).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const TOPBAR = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
	'utf8'
);
const BAUBLE = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/UserBauble.svelte', import.meta.url)),
	'utf8'
);

test('TopBar still passes showLabel to UserBauble on performance', () => {
	assert.match(TOPBAR, /<UserBauble[^>]*\bshowLabel\b/);
});

test('UserBauble renders visible bauble-label text when showLabel is true', () => {
	assert.match(BAUBLE, /showLabel\s*=\s*false/);
	assert.match(BAUBLE, /class="bauble-label"/);
	assert.match(BAUBLE, /Sign in with Google/);
});

test('bauble label flex child can shrink without clipping the control off-screen', () => {
	const styles = BAUBLE.slice(BAUBLE.indexOf('<style>'));
	assert.match(styles, /\.bauble-label[^{]*\{[^}]*min-width:\s*0/s);
	assert.match(styles, /\.bauble\.pilled[^{]*\{[^}]*min-width:\s*0/s);
});
