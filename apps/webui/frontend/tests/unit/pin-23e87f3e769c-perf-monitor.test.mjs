import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('pin 23e87f3e769c openable PerfMeters panel exposes charts and breakdown', async () => {
	const src = await readFile('src/lib/components/rb/PerfMeters.svelte', 'utf8');
	assert.match(src, /let monitorOpen = \$state\(false\)/);
	assert.match(src, /aria-controls="perf-meters-panel"/);
	assert.match(src, /id="perf-meters-panel"/);
	assert.match(src, /import PerfMeterSpark from '\.\/PerfMeterSpark\.svelte'/);
	assert.match(src, /breakdownLines\(/);
});
