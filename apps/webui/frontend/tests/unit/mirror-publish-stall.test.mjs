import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * ui-mirror publish stall detection (#2154).
 *
 * Regression lines:
 * - if a 6 s gap is classified ok then the Playwright AC cannot pass
 * - if GET restamping is how someone "fixes" staleness then AGT still cannot
 *   see a frozen page (that half lives in test_ui_mirror.py)
 * - if the kind is renamed, the console line the AC names is gone
 */

let stall;

before(async () => {
	stall = await loadTypeScriptModule('src/lib/rb/mirror-publish-stall.ts');
});

test('MIRROR_STALL_MS is 5000', () => {
	assert.equal(stall.MIRROR_STALL_MS, 5000);
});

test('classifyMirrorPublishGap treats 5000ms as ok and 5001ms as stall', () => {
	assert.equal(stall.classifyMirrorPublishGap(5000), 'ok');
	assert.equal(stall.classifyMirrorPublishGap(5001), 'stall');
	assert.equal(stall.classifyMirrorPublishGap(0), 'ok');
});

test('classifyMirrorPublishGap rejects non-finite and negative gaps', () => {
	assert.throws(() => stall.classifyMirrorPublishGap(-1), RangeError);
	assert.throws(() => stall.classifyMirrorPublishGap(Number.NaN), RangeError);
	assert.throws(() => stall.classifyMirrorPublishGap(Number.POSITIVE_INFINITY), RangeError);
});

test('mirrorStallMessage names the gap in milliseconds', () => {
	const message = stall.mirrorStallMessage(6123);
	assert.match(message, /6123/);
	assert.match(message, /ms/);
});

test('ui-mirror.ts wires published_at, gap check, and mirror-stall at error', () => {
	const root = fileURLToPath(new URL('../..', import.meta.url));
	const source = readFileSync(`${root}/src/lib/rb/ui-mirror.ts`, 'utf8');
	assert.match(source, /\bpublished_at:/);
	assert.match(source, /classifyMirrorPublishGap/);
	const mirrorStallCall = source.match(/recordPerfEvent\([\s\S]*?['"]mirror-stall['"][\s\S]*?\);/);
	assert.ok(mirrorStallCall, 'recordPerfEvent must be called with mirror-stall');
	assert.match(mirrorStallCall[0], /['"]error['"]/);
});
