import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND_ROOT = fileURLToPath(new URL('../../', import.meta.url));

function read(relPath) {
	return readFileSync(join(FRONTEND_ROOT, relPath), 'utf8');
}

test('audio-quality scan catch returns when isPageGoingAway before console.error', () => {
	const src = read('src/lib/rb/api-audio-quality.ts');
	const idx = src.indexOf('[audio-quality] scan request failed');
	assert.ok(idx >= 0);
	const catchStart = src.lastIndexOf('.catch((error: unknown) => {', idx);
	const slice = src.slice(catchStart, idx + 80);
	assert.match(slice, /if \(isPageGoingAway\(\)\) \{[\s\S]*return;/);
});

test('grid-flags scan catch returns when isPageGoingAway before console.error', () => {
	const src = read('src/lib/rb/api-grid-flags.ts');
	const idx = src.indexOf('[grid-flags] scan request failed');
	assert.ok(idx >= 0);
	const catchStart = src.lastIndexOf('.catch((error: unknown) => {', idx);
	const slice = src.slice(catchStart, idx + 60);
	assert.match(slice, /if \(isPageGoingAway\(\)\) return;/);
});

test('library tags _refreshLoudly returns when isPageGoingAway before pushToast', () => {
	const src = read('src/lib/rb/library-tags.svelte.ts');
	const idx = src.indexOf('tags did not load');
	assert.ok(idx >= 0);
	const fnStart = src.indexOf('function _refreshLoudly()');
	const slice = src.slice(fnStart, idx + 40);
	assert.match(slice, /if \(isPageGoingAway\(\)\) return;/);
});
