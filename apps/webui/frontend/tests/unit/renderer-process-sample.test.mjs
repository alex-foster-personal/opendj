import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const sample = await loadTypeScriptModule('tests/e2e/support/renderer-process-sample.ts');

test('selectRendererProcessIds picks first renderer and gpu-process', () => {
	const ids = sample.selectRendererProcessIds([
		{ id: 111, type: 'browser' },
		{ id: 222, type: 'renderer' },
		{ id: 333, type: 'gpu-process' },
		{ id: 444, type: 'renderer' }
	]);
	assert.equal(ids.rendererPid, 222);
	assert.equal(ids.gpuPid, 333);
});

test('selectRendererProcessIds yields null gpuPid when absent', () => {
	const ids = sample.selectRendererProcessIds([
		{ id: 222, type: 'renderer' },
		{ id: 111, type: 'browser' }
	]);
	assert.equal(ids.rendererPid, 222);
	assert.equal(ids.gpuPid, null);
});

test('selectRendererProcessIds yields null when no renderer entry exists', () => {
	const ids = sample.selectRendererProcessIds([{ id: 1, type: 'browser' }]);
	assert.equal(ids.rendererPid, null);
	assert.equal(ids.gpuPid, null);
});
