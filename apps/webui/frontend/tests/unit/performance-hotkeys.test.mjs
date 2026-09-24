/**
 * /performance keyboard shortcuts (issue #3528).
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('LATENCY-02 Cmd+Space sends quantize when starting a paused deck', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /quantize === true && playing \? \{ quantize: true \}/);
});

test('the global performance listener routes through the shared text-entry predicate', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /handlePerformanceShortcutKeydown\(/);
	assert.match(source, /armPinPlacement/);
	assert.doesNotMatch(source, /isNativeInteractiveTarget/);
});

test('settings hotkeys has no bare single-key branch to gate on the text-entry predicate', async () => {
	// review r3549 P2: this file used to gate its whole listener on
	// isTextEntryTarget, which also blocked the Cmd+, chord and Escape from
	// firing while a settings text field had focus - neither branch here is
	// a bare single key that could edit typed text, so the fix removes the
	// guard entirely rather than narrowing it, unlike the shortcut modules
	// that do keep it for their bare-letter/Space branches.
	const source = await readFile('src/lib/settings/hotkeys.ts', 'utf8');
	assert.doesNotMatch(source, /import\s*\{[^}]*isTextEntryTarget/);
	assert.doesNotMatch(source, /isTextEntryTarget\(/);
});
