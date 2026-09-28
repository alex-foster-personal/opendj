import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('pin 44bd9b44eded Hz and cache perf-meter titles are non-empty derived strings', async () => {
	const src = await readFile('src/lib/components/rb/PerfMeters.svelte', 'utf8');
	const meterBlocks = [...src.matchAll(/<span class="perf-meter"[^>]*>/g)];
	assert.ok(meterBlocks.length >= 2, 'expected at least two compact perf-meter readouts');
	const firstTwo = src.slice(meterBlocks[0].index, meterBlocks[1].index + 200);
	assert.match(firstTwo, /title=\{audioHealthHover\(\)\}/);
	assert.match(src, /class="perf-meter cache-n" title=\{cacheHover\}/);
	assert.match(src, /title=\{waveformStutterHover\(\)\}/);
	assert.match(src, /title=\{memoryHover\}/);
});
