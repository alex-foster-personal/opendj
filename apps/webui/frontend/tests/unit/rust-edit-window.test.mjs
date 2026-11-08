import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';

test('rust load posts out_ms and seeks with quantize:false (no exact) for edit in_ms', async () => {
	const src = await readFrontendSource('src/lib/audio-engine/rust-engine.ts');
	assert.match(src, /\.\.\.\(edit\.out_ms != null \? \{ out_ms: edit\.out_ms \} : \{\}\)/);
	assert.match(src, /position_ms: command\.in_ms,\s*quantize: false/);
	assert.doesNotMatch(src, /exact:\s*true[\s\S]{0,80}command\.in_ms/);
});

test('rust mirror does not dispatch play:false from the rAF tick at edit_out_ms', async () => {
	const src = await readFrontendSource('src/lib/audio-engine/rust-engine.ts');
	const raf = src.slice(
		src.indexOf('function _startRaf'),
		src.indexOf('rafId = requestAnimationFrame(tick);') + 40
	);
	assert.doesNotMatch(raf, /edit_out_ms/);
	assert.doesNotMatch(raf, /type:\s*['"]play['"]/);
});
