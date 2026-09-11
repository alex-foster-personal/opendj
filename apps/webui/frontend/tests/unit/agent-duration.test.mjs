/** AGENT-04 beat-grid duration planning contract. */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const duration = await loadTypeScriptModule('src/lib/rb/agent-duration.ts');

const grid = [
	{ n: 1, time_ms: 0 }, { n: 2, time_ms: 500 }, { n: 3, time_ms: 1000 }, { n: 4, time_ms: 1500 },
	{ n: 1, time_ms: 2000 }, { n: 2, time_ms: 2500 }, { n: 3, time_ms: 3000 }, { n: 4, time_ms: 3500 },
	{ n: 1, time_ms: 4000 }, { n: 2, time_ms: 4500 }, { n: 3, time_ms: 5000 }, { n: 4, time_ms: 5500 },
	{ n: 1, time_ms: 6000 }, { n: 2, time_ms: 6500 }, { n: 3, time_ms: 7000 }, { n: 4, time_ms: 7500 },
	{ n: 1, time_ms: 8000 }, { n: 2, time_ms: 8500 }, { n: 3, time_ms: 9000 }, { n: 4, time_ms: 9500 },
	{ n: 1, time_ms: 10000 }
];

test('four bars resolve from the next downbeat to its real PQTZ target bar', () => {
	assert.deepEqual(
		duration.resolveDuration({ unit: 'bars', n: 4 }, { position_ms: 750, beatgrid: grid, phrases: [] }),
		{ start_ms: 2000, target_ms: 10000 }
	);
});

test('a beat duration with no grid fails as no_grid, never as seconds', () => {
	assert.throws(
		() => duration.resolveDuration({ unit: 'beats', n: 4 }, { position_ms: 0, beatgrid: [], phrases: [] }),
		(error) => error instanceof Error && error.message === 'no_grid'
	);
});

test('phrase duration uses lifted AnlzPhrase boundaries', () => {
	assert.deepEqual(
		duration.resolveDuration(
			{ unit: 'phrases', n: 1 },
			{ position_ms: 1200, beatgrid: grid, phrases: [{ start_ms: 0 }, { start_ms: 4000 }, { start_ms: 8000 }] }
		),
		{ start_ms: 4000, target_ms: 8000 }
	);
});

test('live progress re-plans from the clock position after a tempo change', () => {
	const plan = duration.resolveDuration({ unit: 'bars', n: 4 }, { position_ms: 750, beatgrid: grid, phrases: [] });
	assert.equal(duration.durationProgress(plan, 6000), 0.5);
	assert.equal(duration.durationProgress(plan, 10000), 1);
});

test('an explicit next_downbeat anchor advances beat counting from the selected bar', () => {
	assert.deepEqual(
		duration.resolveDuration({ unit: 'beats', n: 4, anchor: 'next_downbeat' }, { position_ms: 750, beatgrid: grid, phrases: [] }),
		{ start_ms: 2000, target_ms: 4000 }
	);
});
