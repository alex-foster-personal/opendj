// requirement: LIBUX-8673
// [if] 2 or more library rows are selected [then] the readout shows "N selected · ~Xm played · Ym total"
// [if] fewer than 2 rows are selected [then] the readout renders nothing
// [if] the readout is hovered [then] its title says how many tracks used mix points vs the rule of thumb
// [if] the TypeScript estimate runs over the shared fixture [then] it matches the Python estimate case by case
// [if] 500 rows are selected [then] the estimate runs in well under 50 ms
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const fixture = JSON.parse(
	readFileSync(new URL('../../../../../tests/fixtures/played_length_cases.json', import.meta.url), 'utf8')
);

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(`
		export { render } from 'svelte/server';
		export { default as SelectionPlayedLength } from '$lib/components/rb/browser/SelectionPlayedLength.svelte';
		export { estimateTrack, estimatePlayedLength, formatPlayedLength } from '$lib/rb/played-length';
	`);
});

const toInput = (t) => ({
	duration_s: t.duration_s,
	bpm: t.bpm,
	mix_in_s: t.mix_in_s ?? null,
	mix_out_s: t.mix_out_s ?? null
});

test('each fixture case matches the Python estimate', () => {
	for (const c of fixture.cases) {
		const [played, source] = mod.estimateTrack(toInput(c.track));
		assert.ok(Math.abs(played - c.played_s) < 1e-6, `${c.name}: ${played} != ${c.played_s}`);
		assert.equal(source, c.source, c.name);
	}
});

test('the fixture sum matches the Python sum', () => {
	assert.deepEqual(mod.estimatePlayedLength(fixture.cases.map((c) => toInput(c.track))), fixture.total);
});

test('500 rows estimate instantly', () => {
	const tracks = Array.from({ length: 500 }, (_, i) => ({
		duration_s: 240 + (i % 60),
		bpm: 120 + (i % 10),
		mix_in_s: null,
		mix_out_s: null
	}));
	const started = performance.now();
	const result = mod.estimatePlayedLength(tracks);
	assert.ok(performance.now() - started < 50);
	assert.equal(result.from_rule, 500);
});

const row = (order, duration_ms, bpm) => ({ order, duration_ms, bpm });

test('hidden under 2 selected', () => {
	const { body } = mod.render(mod.SelectionPlayedLength, {
		props: { rows: [row(1, 300_000, 120), row(2, 200_000, null)], selectedOrders: [1] }
	});
	assert.doesNotMatch(body, /selected/);
});

test('2 selected shows count, played and total with an explaining title', () => {
	const { body } = mod.render(mod.SelectionPlayedLength, {
		props: { rows: [row(1, 300_000, 120), row(2, 200_000, null), row(3, 100_000, 120)], selectedOrders: [1, 2] }
	});
	assert.match(body, /2 selected · ~6m played · 8m total/);
	assert.match(body, /title="[^"]*0 with mix points, 2 by rule of thumb/);
});
