// A StemWaveRow-shaped $effect calling ensureStemWaveform on a READY entry
// must settle. The LRU retouch writes the entry the effect just read, so a
// tracked read re-runs the effect forever: effect_update_depth_exceeded, and
// the whole page stops updating (stem buttons dead, recovery dialog frozen).
// Runs the REAL Svelte scheduler via load-rune-module.mjs. -Claude
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const originalFetch = globalThis.fetch;
after(() => {
	globalThis.fetch = originalFetch;
});

const ENTRY = [
	"export { ensureStemWaveform, getStemWaveformEntry } from '$lib/components/rb/wave/stem-waveform-cache.svelte';",
	"import { flushSync } from 'svelte';",
	"import { ensureStemWaveform } from '$lib/components/rb/wave/stem-waveform-cache.svelte';",
	'export function runRowEffect(sid: string, part: string): { runs: number; error: unknown; stop: () => void } {',
	'	const out = { runs: 0, error: null as unknown, stop: () => {} };',
	'	try {',
	'		out.stop = $effect.root(() => { $effect(() => { out.runs += 1; ensureStemWaveform(sid, part); }); });',
	'		flushSync();',
	'	} catch (error) {',
	'		out.error = error;',
	'	}',
	'	return out;',
	'}'
].join('\n');

function envelopeResponse(stableId, part) {
	return new Response(
		JSON.stringify({
			schema: 1,
			stable_id: stableId,
			part,
			layout: 'demucs4',
			points: 4,
			envelope: [0, 0.5, 1, 0.25]
		}),
		{ status: 200, headers: { 'content-type': 'application/json' } }
	);
}

function countingFetch() {
	const calls = [];
	globalThis.fetch = async (url) => {
		calls.push(String(url));
		const [, sid, part] = String(url).match(/tracks\/([^/]+)\/stems\/([^/]+)\/waveform$/);
		return envelopeResponse(sid, part);
	};
	return calls;
}

async function readyEntry(live, sid, part) {
	live.ensureStemWaveform(sid, part);
	await new Promise((resolve) => setTimeout(resolve, 0));
	const entry = live.getStemWaveformEntry(sid, part);
	assert.equal(entry?.status, 'ready', 'setup: entry must be ready before the effect runs');
	return entry;
}

test('if a StemWaveRow-style effect over a ready stem waveform loops forever then broken', async () => {
	const live = await loadRuneModule(ENTRY);
	countingFetch();
	await readyEntry(live, 'loop-track', 'drums');
	const row = live.runRowEffect('loop-track', 'drums');
	row.stop();
	assert.equal(row.error, null, `effect did not settle: ${String(row.error)}`);
	assert.equal(row.runs, 1, 'the effect must run once, not re-trigger itself');
});

test('if ensureStemWaveform from an effect stops refreshing the LRU touch then broken', async () => {
	const live = await loadRuneModule(ENTRY);
	countingFetch();
	const before = (await readyEntry(live, 'touch-track', 'bass')).touched;
	const row = live.runRowEffect('touch-track', 'bass');
	row.stop();
	assert.equal(row.error, null, String(row.error));
	const after = live.getStemWaveformEntry('touch-track', 'bass').touched;
	assert.ok(
		after > before,
		`a visible row must stay most-recently-used (touched ${before} -> ${after})`
	);
});

test('if an effect over an uncached stem waveform does not fetch exactly once then broken', async () => {
	const live = await loadRuneModule(ENTRY);
	const calls = countingFetch();
	const row = live.runRowEffect('fresh-track', 'vocals');
	await new Promise((resolve) => setTimeout(resolve, 0));
	row.stop();
	assert.equal(row.error, null, String(row.error));
	assert.equal(calls.length, 1, `expected one fetch, saw ${calls.length}`);
	assert.equal(live.getStemWaveformEntry('fresh-track', 'vocals')?.status, 'ready');
});
