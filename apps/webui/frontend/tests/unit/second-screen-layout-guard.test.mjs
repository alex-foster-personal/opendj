import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let preset;

before(async () => {
	preset = await loadTypeScriptModule('src/lib/rb/performance-preset.ts');
});

test('isSecondScreenRoutePath matches /second-screen only', () => {
	assert.equal(preset.isSecondScreenRoutePath('/second-screen'), true);
	assert.equal(preset.isSecondScreenRoutePath('/second-screen/foo'), true);
	assert.equal(preset.isSecondScreenRoutePath('/'), false);
	assert.equal(preset.isSecondScreenRoutePath('/performance'), false);
	assert.equal(preset.isSecondScreenRoutePath('/second-screenx'), false);
});

test('rootLayoutRunsMainWindowLoops is false on second screen', () => {
	assert.equal(preset.rootLayoutRunsMainWindowLoops('/second-screen'), false);
	assert.equal(preset.rootLayoutRunsMainWindowLoops('/performance'), true);
	assert.equal(preset.rootLayoutRunsMainWindowLoops('/'), true);
	assert.equal(preset.rootLayoutRunsMainWindowLoops('/music-player'), true);
});
