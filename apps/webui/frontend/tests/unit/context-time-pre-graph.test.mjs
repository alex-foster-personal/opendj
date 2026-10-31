import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * [if] queryPerformanceState runs before the audio graph exists [then]
 * contextTimeNowSec must not throw a window error (#6649), [else stop].
 */
test('contextTimeNowSec returns +Infinity instead of throwing when the graph is absent', () => {
	const source = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const block = source.match(/contextTimeNowSec\(\): number \{[\s\S]*?\n\t\}/);
	assert.ok(block, 'contextTimeNowSec must exist on the engine');
	assert.doesNotMatch(
		block[0],
		/contextTimeNowSec: audio graph not initialised/,
		'if contextTimeNowSec throws before the graph exists then IPC query floods window-error'
	);
	assert.match(block[0], /if \(_ctx === null\) return Number\.POSITIVE_INFINITY;/);
});

test('engine.contextTimeNowSec does not throw before the audio graph exists', async () => {
	const { engine } = await loadTypeScriptModule('tests/unit/fixtures/trackify-session-entry.ts');
	assert.equal(engine.contextTimeNowSec(), Number.POSITIVE_INFINITY);
});
