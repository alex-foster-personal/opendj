/**
 * pin 25d6dea6: the deferred-seek ghost cursor must BLINK while a waveform
 * seek is armed. It was frozen: WaveRow held the blink phase in a `$derived`
 * whose only input was `performance.now()`, which is not reactive, so the
 * phase was computed once at mount and never again.
 *
 * - if the blink phase is read once instead of per painted frame then the
 *   ghost is permanently on or permanently off -> broken
 * - if the phase is not part of the repaint inputs then a sub-pixel scroll
 *   skips the frame that should toggle the ghost -> broken
 * - if a disarmed deck still reports a visible ghost then a stale cursor
 *   paints with no pending seek -> broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const waveRowPath = path.join(here, '../../src/lib/components/rb/wave/WaveRow.svelte');

let overlays;
before(async () => {
	overlays = await loadTypeScriptModule('src/lib/components/rb/wave/wave-row-deckux-overlays.ts');
});

test('ghost seek frame toggles visibility as the frame clock advances', () => {
	const armed = { target_position_ms: 60000, remaining_ms: 900 };
	const seen = new Set();
	for (let nowMs = 0; nowMs < 480; nowMs += 16) {
		const frame = overlays.ghostSeekFrame(armed, nowMs);
		assert.equal(frame.ghostSeekMs, 60000);
		seen.add(frame.ghostSeekVisible);
		assert.equal(frame.blinkPhase, Math.floor(nowMs / 120));
	}
	assert.deepEqual([...seen].sort(), [false, true], 'both blink phases must occur within 480ms');
});

test('ghost seek frame is hidden with no armed seek whatever the clock says', () => {
	for (const nowMs of [0, 60, 120, 180, 240]) {
		const frame = overlays.ghostSeekFrame(null, nowMs);
		assert.equal(frame.ghostSeekMs, null);
		assert.equal(frame.ghostSeekVisible, false);
		assert.equal(frame.blinkPhase, null, 'a disarmed deck must not force repaints on a blink clock');
	}
});

test('WaveRow evaluates the blink per painted frame and repaints on a phase change', () => {
	const src = readFileSync(waveRowPath, 'utf8');
	assert.ok(
		!/\$derived\(\s*ghostSeekBlinkVisible\(/.test(src),
		'a $derived over performance.now() has no reactive dependency and freezes the blink'
	);
	assert.match(
		src,
		/const ghost = ghostSeekFrame\(waveformSeekArmed, performance\.now\(\)\);/,
		'draw() must compute the ghost frame from the frame clock'
	);
	assert.match(
		src,
		/const visualInputs = \[[\s\S]*?ghost\.blinkPhase[\s\S]*?\] as const;/,
		'the blink phase must be a repaint input so a still waveform repaints on the toggle'
	);
});
