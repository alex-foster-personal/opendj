/**
 * Registration guards only. Issue5761's behavior is exercised by the real
 * /performance browser in tests/live/performance-focused-controls.mjs.
 * Supersedes the FakeElement/handler stand-ins and fabricated feedback state
 * that could not prove native activation or unchanged real transport.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('performance-hotkeys registers keydown in capture phase so body Space cannot scroll', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /addEventListener\('keydown', onKey, \{ capture: true \}\)/);
});

test('performance-hotkeys uses shared routing rather than a second focus check', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /handlePerformanceShortcutKeydown/);
	assert.doesNotMatch(source, /isNativeInteractiveTarget/);
});
