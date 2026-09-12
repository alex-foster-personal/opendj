// requirement: NATIVE-03
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/** Truth table for hasTrustedBeatGrid (nav1-consumers item 1). */

const REAL_BEATS = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.552 }
];

function twoTempoBeats(firstBpm, firstCount, secondBpm, secondCount) {
	const beats = [];
	let t = 0;
	const total = firstCount + secondCount;
	for (let i = 0; i < total; i++) {
		const bpm = i < firstCount ? firstBpm : secondBpm;
		beats.push({ n: (i % 4) + 1, bpm, t });
		t += 60 / bpm;
	}
	return beats;
}

const MULTI_ANCHOR_BEATS = twoTempoBeats(120, 80, 140, 80);
const MULTI_ANCHOR_TEMPO_CHANGES = [
	{ at_s: 40, bpm_before: 120, bpm_after: 140, confidence: 0.9 }
];

function assertMultiAnchorTrusted(grid, payload) {
	assert.equal(grid.hasTrustedBeatGrid(payload), true);
	assert.equal(grid.hasRealBeatGrid(payload.beatgrid.beats), true);
	assert.equal(grid.shouldPaintBeatGrid(payload), true);
	const st = { quantize_enabled: true, beat_sync_enabled: true, stable_id: 'x', anlz: payload };
	assert.equal(grid.effectiveQuantize(st), true);
	assert.equal(grid.effectiveBeatSync(st), true);
	assert.equal(grid.gridFeaturesInert(st), false);
}

let grid;

before(async () => {
	grid = await loadTypeScriptModule('src/lib/player/grid-features.ts');
});

function anlz(overrides) {
	return {
		beatgrid: {
			source: 'rekordbox',
			beat_count: REAL_BEATS.length,
			beats: REAL_BEATS,
			...overrides.beatgrid
		},
		tempo_changes: overrides.tempo_changes
	};
}

test('rekordbox with real beats is trusted', () => {
	assert.equal(grid.hasTrustedBeatGrid(anlz({ beatgrid: { source: 'rekordbox' } })), true);
});

test('own ok with static_grid_untrusted false is trusted', () => {
	assert.equal(
		grid.hasTrustedBeatGrid(
			anlz({
				beatgrid: {
					source: 'own',
					status: 'ok',
					static_grid_untrusted: false
				}
			})
		),
		true
	);
});

test('own ok dynamic grid with omitted static_grid_untrusted is trusted', () => {
	const payload = anlz({
		beatgrid: { source: 'own', status: 'ok' },
		tempo_changes: [{ at_s: 32.5, bpm_before: 120, bpm_after: 128, confidence: 0.9 }]
	});
	assert.equal(grid.hasTrustedBeatGrid(payload), true);
	assert.equal(grid.shouldPaintBeatGrid(payload), true);
	const st = { quantize_enabled: true, beat_sync_enabled: true, stable_id: 'x', anlz: payload };
	assert.equal(grid.effectiveQuantize(st), true);
	assert.equal(grid.effectiveBeatSync(st), true);
});

test('own failed with beats is not trusted and does not paint', () => {
	const payload = anlz({
		beatgrid: { source: 'own', status: 'failed', reason: 'no_trackable_pulse' }
	});
	assert.equal(grid.hasTrustedBeatGrid(payload), false);
	assert.equal(grid.shouldPaintBeatGrid(payload), false);
});

test('own ok with static_grid_untrusted true is not trusted but still paints beats', () => {
	const payload = anlz({
		beatgrid: { source: 'own', status: 'ok', static_grid_untrusted: true },
		tempo_changes: [{ at_s: 32.5, bpm_before: 120, bpm_after: 128, confidence: 0.9 }]
	});
	assert.equal(grid.hasTrustedBeatGrid(payload), false);
	assert.equal(grid.shouldPaintBeatGrid(payload), true);
	const st = { quantize_enabled: true, beat_sync_enabled: true, stable_id: 'x', anlz: payload };
	assert.equal(grid.effectiveQuantize(st), false);
	assert.equal(grid.effectiveBeatSync(st), false);
	assert.equal(grid.gridFeaturesInert(st), true);
	assert.match(grid.gridFeatureInertTip({ stable_id: 'x', anlz: payload }), /32\.500s/);
});

test('missing or unknown beatgrid.source fails closed', () => {
	assert.equal(
		grid.hasTrustedBeatGrid(anlz({ beatgrid: { source: undefined, beats: REAL_BEATS } })),
		false
	);
	assert.equal(grid.hasTrustedBeatGrid(anlz({ beatgrid: { source: 'legacy', beats: REAL_BEATS } })), false);
});

test('effectiveQuantize requires trusted grid not merely real beats', () => {
	const st = {
		quantize_enabled: true,
		stable_id: 't1',
		anlz: anlz({
			beatgrid: { source: 'own', status: 'failed', reason: 'no_trackable_pulse' }
		})
	};
	assert.equal(grid.effectiveQuantize(st), false);
});

test('own ok omitted static_grid_untrusted with varying bpm multi-anchor map is trusted', () => {
	const payload = anlz({
		beatgrid: {
			source: 'own',
			status: 'ok',
			beats: MULTI_ANCHOR_BEATS,
			beat_count: MULTI_ANCHOR_BEATS.length
		},
		tempo_changes: MULTI_ANCHOR_TEMPO_CHANGES
	});
	assertMultiAnchorTrusted(grid, payload);
});

test('own ok static_grid_untrusted false with varying bpm multi-anchor map is trusted', () => {
	const payload = anlz({
		beatgrid: {
			source: 'own',
			status: 'ok',
			static_grid_untrusted: false,
			beats: MULTI_ANCHOR_BEATS,
			beat_count: MULTI_ANCHOR_BEATS.length
		},
		tempo_changes: MULTI_ANCHOR_TEMPO_CHANGES
	});
	assertMultiAnchorTrusted(grid, payload);
});

test('own ok static_grid_untrusted true with varying bpm multi-anchor map stays untrusted', () => {
	const payload = anlz({
		beatgrid: {
			source: 'own',
			status: 'ok',
			static_grid_untrusted: true,
			beats: MULTI_ANCHOR_BEATS,
			beat_count: MULTI_ANCHOR_BEATS.length
		},
		tempo_changes: MULTI_ANCHOR_TEMPO_CHANGES
	});
	assert.equal(grid.hasTrustedBeatGrid(payload), false);
	assert.equal(grid.shouldPaintBeatGrid(payload), true);
	const st = { quantize_enabled: true, beat_sync_enabled: true, stable_id: 'x', anlz: payload };
	assert.equal(grid.effectiveQuantize(st), false);
	assert.equal(grid.effectiveBeatSync(st), false);
	assert.equal(grid.gridFeaturesInert(st), true);
});
