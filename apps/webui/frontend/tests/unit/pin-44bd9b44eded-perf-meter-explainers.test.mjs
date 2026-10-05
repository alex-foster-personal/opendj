import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

// PERF-UI-10 moved the per-readout hover strings off `title` and into the one
// shared card, so the pin now follows them there: each live explainer must
// still reach the card's row model.
test('pin 44bd9b44eded every perf-meter explainer string still feeds the shared card', async () => {
	const src = await readFile('src/lib/components/rb/PerfMeters.svelte', 'utf8');
	assert.match(src, /perfReadoutRows\(\{/);
	assert.match(src, /hzDetail: audioHealthHover\(\)/);
	assert.match(src, /cacheDetail: cacheHover/);
	assert.match(src, /waveformDetail: waveformStutterHover\(\)/);
	assert.match(src, /memoryDetail: memoryHover/);
});
