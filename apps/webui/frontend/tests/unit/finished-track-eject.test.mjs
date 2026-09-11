import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { compile } from 'svelte/compiler';

const filename = new URL('../../src/lib/components/rb/wave/WaveRow.svelte', import.meta.url);
const source = readFileSync(filename, 'utf8');

test('finished-track eject waits for silent settled output at the actual track end', () => {
	assert.ok(source.includes('const finished = $derived('));
	const condition = source.slice(source.indexOf('const finished ='), source.indexOf('// ---- anlz source'));
	for (const guard of ['deck.stable_id !== null', 'deck.duration_ms > 0', 'deck.position_ms >= deck.duration_ms', '!deck.audible', '!deck.playing', '!deck.transport_pending']) {
		assert.ok(condition.includes(guard), `missing finished-state guard: ${guard}`);
	}
});

test('finished-track button uses the same unload dispatcher as deck artwork', () => {
	assert.match(source, /\{#if finished\}[\s\S]*class="finished-eject"/);
	assert.ok(source.includes("runPerformanceCommandFromUi({ type: 'unload', deck: deckId })"));
	compile(source, { filename: filename.pathname, generate: 'client' });
});
