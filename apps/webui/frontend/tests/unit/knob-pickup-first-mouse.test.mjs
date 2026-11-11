/**
 * requirement: MIXUX-10355
 *
 * Issue #9547: Knob pickup UX pins live in knob-control.test.mjs (GUARD-15 allowlist).
 *
 * [if] the desktop main window builder stops calling accept_first_mouse(true)
 * [then] fail, [else stop].
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const shell = readFileSync(
	fileURLToPath(new URL('../../../../desktop/src-tauri/src/main.rs', import.meta.url)),
	'utf8'
);

test('the desktop main window accepts the first mouse, so the first press turns a dial', () => {
	const builder = shell.slice(shell.indexOf('WebviewWindowBuilder::new(app, MAIN_WINDOW_LABEL'));
	const chain = builder.slice(
		0,
		builder.indexOf('.build()') > 0 ? builder.indexOf('.build()') : 4000
	);
	assert.match(chain, /\.accept_first_mouse\(true\)/);
});
