/**
 * PERFMODE-15 leak capture line reader
 * (`scripts/perf/trackify-quiescent-checkpoint.mjs`,
 * ADR-NEW-trackify-leak-kpi-quiescent-baselines).
 *
 * Only the stdin line reader is tested here, over a real stream. Quiescing,
 * resuming and the CHECKPOINT/RESUME protocol are tested against the real
 * Trackify page, the real dispatcher and real Chromium in
 * tests/perf/test_capture_mode_ratios_leak.py: the fake page these tests used
 * to drive could not catch a broken dispatcher, autoplay controller or
 * garbage-collection path (Codex P1 r4171125798, PR #4888).
 */
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { test } from 'node:test';

import { createLineReader } from '../../../../../scripts/perf/trackify-quiescent-checkpoint.mjs';

test('the line reader yields lines in order, including ones that arrived before they were asked for', async () => {
	const input = new PassThrough();
	const reader = createLineReader(input);
	input.write('CHECKPOINT\nRESUME\n');
	assert.equal(await reader.next(), 'CHECKPOINT');
	assert.equal(await reader.next(), 'RESUME');
	const pending = reader.next();
	input.write('NEXT\n');
	assert.equal(await pending, 'NEXT');
	input.end();
	await assert.rejects(reader.next(), /stdin closed before the next protocol line/);
});
