import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const registry = await loadTypeScriptModule('src/lib/rb/audio-context-registry.ts');

test('register and unregister audio contexts', () => {
	registry.resetAudioContextRegistryForTest();
	const ctx = { close: () => Promise.resolve() };
	registry.registerAudioContext(ctx);
	assert.equal(registry.countRegisteredAudioContexts(), 1);
	registry.unregisterAudioContext(ctx);
	assert.equal(registry.countRegisteredAudioContexts(), 0);
});
