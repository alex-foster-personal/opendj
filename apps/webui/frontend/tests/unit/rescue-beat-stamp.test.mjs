import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// requirement: RESCUE-01

async function _loadBeatStamp() {
	return loadTypeScriptModule('src/lib/rb/rescue-beat-stamp.ts');
}

function _grid120Bpm(seconds = 8) {
	const beats = [];
	for (let i = 0; i < seconds * 2; i += 1) {
		beats.push({ n: ((i % 4) + 1), t: i * 0.5 });
	}
	return beats;
}

test('beat stamp round-trips within 1/64 beat on a fixture grid', async () => {
	const { encodeBeatStamp, decodeBeatStamp } = await _loadBeatStamp();
	const beats = _grid120Bpm();
	const beatIntervalMs = 500;
	const tolerance = beatIntervalMs / 64;
	for (const positionMs of [750, 1250, 2100, 3333]) {
		const stamp = encodeBeatStamp(beats, positionMs);
		assert.equal(stamp.kind, 'beatgrid');
		const decoded = decodeBeatStamp(stamp, beats);
		assert.ok(decoded !== null);
		assert.ok(Math.abs(decoded - positionMs) <= tolerance);
	}
});

test('beat stamp falls back to sample when grid is absent', async () => {
	const { encodeBeatStamp, decodeBeatStamp } = await _loadBeatStamp();
	const stamp = encodeBeatStamp([], 1234);
	assert.deepEqual(stamp, { kind: 'sample', position_ms: 1234 });
	assert.equal(decodeBeatStamp(stamp, []), 1234);
});

test('decode returns null when beatgrid stamp is out of grid span', async () => {
	const { decodeBeatStamp } = await _loadBeatStamp();
	const beats = _grid120Bpm(2);
	assert.equal(
		decodeBeatStamp({ kind: 'beatgrid', beat_index: 99, beat_n: 1, phase: 0 }, beats),
		null
	);
});
