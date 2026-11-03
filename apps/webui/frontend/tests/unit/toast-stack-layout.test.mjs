/**
 * @pytest.mark.requirement UX-TOAST-03
 * [if] toast stack CSS is read [then] it caps width at one third viewport [else stop].
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relative) {
	return readFileSync(fileURLToPath(new URL(`../../src/${relative}`, import.meta.url)), 'utf8');
}

const APP_CSS = source('app.css');

test('toast stack caps width at one third of the viewport', () => {
	const block = APP_CSS.match(/\.toast-stack\s*\{[^}]+\}/)?.[0] ?? '';
	assert.match(block, /max-width:\s*min\(33vw/);
	assert.match(APP_CSS, /\.toast\s*\{[^}]*max-width:\s*100%/);
});

test('the toast tray sits left of the open assistant sidebar', () => {
	const shifted = /right:\s*calc\(1rem \+ var\(--assistant-dock-width, 0px\)\)/;
	assert.match(APP_CSS.match(/\.toast-stack\s*\{[^}]+\}/)?.[0] ?? '', shifted);
});
