// Codex P1 BLOCKING, PR #6235: a grid edit holds persistence-N while its
// refresh re-reads /anlz; the decks' adoption of the new grid then claims [N].
// A preset queued in between claims [N] and persistence-N, so it waits for the
// edit. If the refresh awaited the adoption, the edit would wait for the
// preset and neither would ever settle. Real scheduler, real anlz cache.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const beats = [
	{ bpm: 128, n: 1, t: 0 },
	{ bpm: 128, n: 2, t: 0.46875 }
];
const payload = {
	stable_id: 'deadlock-track',
	points: 38400,
	waveform: {
		kind: 'mono',
		preview: { length: 0, low: [], mid: [], high: [] },
		detail: { length: 0, low: [], mid: [], high: [] }
	},
	beatgrid: { source: 'own', status: 'ok', reason: null, beat_count: beats.length, beats },
	cues: [],
	phrases: [],
	local_waveform: { status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 },
	vocals: { status: 'not_analyzed' }
};

const within = (promise, ms, what) =>
	Promise.race([
		promise,
		new Promise((_, reject) => setTimeout(() => reject(new Error(`${what} never settled`)), ms))
	]);

test('a preset queued behind an in-flight grid edit does not deadlock with the grid adoption', async () => {
	const cache = await loadTypeScriptModule('src/lib/components/rb/wave/anlz-cache.svelte.ts', {
		viteApiBase: 'https://grid-edit-deadlock.example.test'
	});
	const { ScopedCommandScheduler } = await loadTypeScriptModule(
		'src/lib/rb/performance-command-scheduler.ts'
	);
	const scheduler = new ScopedCommandScheduler();
	const order = [];
	cache.installAuthoritativeAnlzGridSink((stable_id) =>
		scheduler.run([1], async () => {
			order.push(`adopt ${stable_id}`);
		})
	);
	let answer;
	const answered = new Promise((resolve) => (answer = resolve));
	const originalFetch = globalThis.fetch;
	globalThis.fetch = async () => {
		await answered;
		return new Response(JSON.stringify(payload), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	try {
		const edit = scheduler.run(['persistence-1'], async () => {
			await cache.refetchAnlzCacheEntry('deadlock-track');
			order.push('edit');
		});
		await new Promise((resolve) => setTimeout(resolve, 0));
		const preset = scheduler.run([1, 'persistence-1'], async () => {
			order.push('preset');
		});
		answer();
		await within(Promise.all([edit, preset]), 2000, 'the grid edit and the preset');
		await within(
			scheduler.run([1], async () => {}),
			2000,
			'the adoption'
		);
		assert.deepEqual(order, ['edit', 'preset', 'adopt deadlock-track']);
		assert.deepEqual(
			cache.getAnlzEntry('deadlock-track').data.beatgrid.beats,
			beats,
			'the edited grid is published'
		);
	} finally {
		globalThis.fetch = originalFetch;
	}
});
