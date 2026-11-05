// requirement: LIBUX-8682
// [if] two neighbouring non-skipped rows are a gross clash [then] the lower row gets an edge titled "Key clash 8A -> 3B (5 steps)"
// [if] a skipped row sits between them [then] the edge still lands on the lower row (skips bridged)
// [if] the clash is not gross, or the rows are not neighbours on screen (sorted view) [then] no edge
// [if] TrackTable renders [then] the edge spans only the Key and BPM cells and is a jagged red line
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const here = dirname(fileURLToPath(import.meta.url));
let mod;

before(async () => {
	mod = await loadSvelteSsrModule(
		"export { grossClashEdges, clashTitle } from '$lib/rb/set-clash-marker';\nexport { summarizeSet } from '$lib/rb/set-target';"
	);
});

const entries = [
	{ stable_id: 'a', duration_s: 300, bpm: 128, key: '8A' },
	{ stable_id: 'b', duration_s: 300, bpm: 128, key: '9A', skipped: true },
	{ stable_id: 'c', duration_s: 300, bpm: 128, key: '3B' },
	{ stable_id: 'd', duration_s: 300, bpm: 128, key: '4B' },
	{ stable_id: 'e', duration_s: 300, bpm: 128, key: '5A' },
	{ stable_id: 'f', duration_s: 300, bpm: 128, key: null },
	{ stable_id: 'g', duration_s: 300, bpm: 128, key: '11B' }
];

test('gross clashes land on the lower row, skips bridged, unknown keys ignored', () => {
	const { clashes } = mod.summarizeSet(entries, null);
	const edges = mod.grossClashEdges(['a', 'b', 'c', 'd', 'e', 'f', 'g'], new Set(['b']), clashes);
	// a(8A) -> c(3B) is 5 steps (gross); c -> d 1 step same letter; d(4B) -> e(5A) 1 step letter change (not gross).
	assert.deepEqual([...edges.entries()], [['c', 'Key clash 8A -> 3B (5 steps)']]);
});

test('a sorted view that separates the pair draws nothing', () => {
	const { clashes } = mod.summarizeSet(entries, null);
	assert.equal(mod.grossClashEdges(['c', 'a', 'd'], new Set(['b']), clashes).size, 0);
});

test('TrackTable draws the edge in the Key and BPM cells only', () => {
	const table = readFileSync(join(here, '../../src/lib/components/rb/browser/TrackTable.svelte'), 'utf8');
	const uses = table.match(/<span class="set-clash-edge"[^>]*>/g) ?? [];
	assert.equal(uses.length, 2, 'one edge span in each of the Key and BPM cells');
	// The Key cell is a hover-card host, so its clash reason rides the card note (no title=);
	// the BPM cell keeps the reason on its own title.
	assert.equal(uses.filter((span) => span.includes('title={clashEdges.get(row.stable_id)}')).length, 1);
	assert.match(table, /clashEdges\.get\(row\.stable_id\) \?\? null\]/);
	const rule = /\.set-clash-edge\s*\{([^}]*)\}/.exec(table);
	assert.ok(rule, 'set-clash-edge style rule');
	assert.match(rule[1], /top:/);
	assert.match(rule[1], /--rb-red|#d9434e|red/);
	assert.match(rule[1], /linear-gradient\(135deg[\s\S]*linear-gradient\(45deg|zigzag|svg/);
});
