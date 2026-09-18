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

test('settings hotkeys use the shared text-entry predicate', async () => {
	const source = await readFile('src/lib/settings/hotkeys.ts', 'utf8');
	assert.match(source, /isTextEntryTarget\(e\.target\)/);
});
