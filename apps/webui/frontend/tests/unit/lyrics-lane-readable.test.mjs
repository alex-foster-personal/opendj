/**
 * @pytest.mark.requirement LYRICS-10
 * Pin 6b1da5a8c6ad: waveform lyric lines did not meet the playhead as they
 * were sung, had no readable backing, and ran over each other.
 *
 * [if] a line starts at the playhead time [then] the START of its text sits
 *   on the playhead, not its middle
 * [if] two lines follow each other [then] the first is no wider than the
 *   distance to the second, so they cannot overlap
 * [if] a line is the last one [then] it has no width cap
 * [if] a line is drawn [then] it sits on a mostly opaque backing
 *
 * Synthetic placeholder words only; no real lyric text.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const LANE = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/wave/LyricsLane.svelte', import.meta.url)),
	'utf8'
);
const WINDOW_S = 24;

let lane;
let ssr;
before(async () => {
	lane = await loadTypeScriptModule('src/lib/components/rb/wave/lyrics-lane.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as LyricsLane } from '$lib/components/rb/wave/LyricsLane.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

test('a line is as wide as the gap to the next line, scaled by pitch', () => {
	const width = lane.lyricLaneWidthPercent({
		lineStartMs: 10_000,
		nextStartMs: 13_000,
		pitch: 1,
		windowSeconds: WINDOW_S
	});
	assert.equal(width, 12.5);
	assert.equal(
		lane.lyricLaneWidthPercent({
			lineStartMs: 10_000,
			nextStartMs: 13_000,
			pitch: 2,
			windowSeconds: WINDOW_S
		}),
		6.25
	);
});

test('the last line has no width cap', () => {
	assert.equal(
		lane.lyricLaneWidthPercent({
			lineStartMs: 10_000,
			nextStartMs: null,
			pitch: 1,
			windowSeconds: WINDOW_S
		}),
		null
	);
});

test('a next line that does not come later is refused, not drawn at zero width', () => {
	assert.throws(
		() =>
			lane.lyricLaneWidthPercent({
				lineStartMs: 10_000,
				nextStartMs: 10_000,
				pitch: 1,
				windowSeconds: WINDOW_S
			}),
		/must start later/
	);
});

test('left edge plus width never passes the next left edge', () => {
	const starts = [1_000, 1_400, 4_000, 4_050, 9_000];
	for (const positionMs of [0, 2_000, 8_500]) {
		for (let i = 0; i < starts.length - 1; i += 1) {
			const args = { positionMs, pitch: 1.06, windowSeconds: WINDOW_S };
			const left = lane.lyricLanePositionPercent({ lineStartMs: starts[i], ...args });
			const nextLeft = lane.lyricLanePositionPercent({ lineStartMs: starts[i + 1], ...args });
			const width = lane.lyricLaneWidthPercent({
				lineStartMs: starts[i],
				nextStartMs: starts[i + 1],
				pitch: 1.06,
				windowSeconds: WINDOW_S
			});
			assert.ok(left + width <= nextLeft + 1e-9, `line ${i} overlaps line ${i + 1}`);
		}
	}
});

test('the rendered line starts at the playhead and is capped to the next line on its row', () => {
	// Two-row layout (LYR-09, decision G, Mon 5 Oct 2026): entry i sits on row
	// i % 2, so a line is capped at the next line on ITS row (i + 2), less the
	// entry gap; the neighbour on the other row cannot collide with it.
	const html = ssr.render(ssr.LyricsLane, {
		props: {
			lyrics: {
				lines: [
					{ start_ms: 5_000, text: 'alpha' },
					{ start_ms: 6_000, text: 'bravo' },
					{ start_ms: 8_000, text: 'charlie' }
				]
			},
			loadError: null,
			positionMs: 5_000,
			pitch: 1
		}
	}).body;
	const spans = [...html.matchAll(/<span[^>]*class="lyric-line[^"]*"[^>]*>/g)].map((m) => m[0]);
	assert.equal(spans.length, 3);
	assert.match(spans[0], /left:\s*50%/);
	assert.match(spans[0], /data-row="0"/);
	assert.match(spans[1], /data-row="1"/);
	assert.match(spans[0], /max-width:\s*calc\(12\.5% - 6px\)/);
	assert.doesNotMatch(spans[1], /max-width/);
	assert.doesNotMatch(spans[2], /max-width/);
});

test('the line is left-anchored on the skin lyric box', () => {
	// Decision G (Mon 5 Oct 2026) replaced the 85% app-background backing with
	// a black box at --rb-lyric-box-alpha plus a black outline, over the wave.
	const rule = LANE.slice(LANE.indexOf('.lyric-line {'), LANE.indexOf('.lyric-line.active'));
	assert.doesNotMatch(rule, /translateX\(-50%\)/);
	assert.match(rule, /text-overflow:\s*ellipsis/);
	assert.match(rule, /background:\s*rgb\(0 0 0 \/ var\(--rb-lyric-box-alpha\)\)/);
	assert.match(rule, /text-shadow:[^;]*var\(--rb-lyric-outline-px\)/);
});
