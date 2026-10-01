import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let beatgridFallback;
let gridFeatures;
let waveMath;

before(async () => {
	beatgridFallback = await loadTypeScriptModule('src/lib/rb/beatgrid-fallback.ts');
	gridFeatures = await loadTypeScriptModule('src/lib/player/grid-features.ts');
	waveMath = await loadTypeScriptModule('src/lib/components/rb/wave/wave-math.ts');
});

/** /anlz 200 payload with the grid the caller cares about. Only `beatgrid`
 * is read by the gate; the rest is the real empty-payload shape the daemon
 * serves for a track with no rekordbox analysis (rb_vendor.empty_anlz_payload). */
function anlzWithBeats(beats) {
	const emptyBands = { length: 0, low: [], mid: [], high: [] };
	return {
		stable_id: 'abc123',
		points: 38400,
		waveform: { kind: 'mono', preview: emptyBands, detail: emptyBands },
		beatgrid: { beat_count: beats.length, beats },
		cues: [],
		phrases: []
	};
}

const REAL_GRID = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 }
];

test('shouldUseBeatgridFallback fires once /anlz reports the ANLZ file is gone', () => {
	// A rekordbox-MAPPED track whose ANLZ file is missing: no payload to read.
	const gate = { anlzErrorCode: 'ANALYSIS_NOT_FOUND', anlz: null, vendor: null, effectiveSource: 'own' };
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), true);
});

test('any other /anlz error stays out of this lane', () => {
	for (const code of ['SOME_OTHER_ERROR', 'STATE_DB_UNAVAILABLE']) {
		const gate = { anlzErrorCode: code, anlz: null, vendor: null, effectiveSource: 'own' };
		assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), false);
	}
});

test('an unmapped local track with an empty /anlz grid reaches the fallback', () => {
	// The parity gap: /anlz answers 200 with an empty grid for a locally
	// imported file, so the ANALYSIS_NOT_FOUND-only gate never fired.
	const gate = { anlzErrorCode: null, anlz: anlzWithBeats([]), vendor: 'local', effectiveSource: 'own' };
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), true);
});

test('a rekordbox-mapped track with an empty grid never reaches the fallback', () => {
	// rekordbox owns this track's grid; an absent PQTZ is rekordbox's answer,
	// not an invitation to substitute our own measurement.
	const gate = { anlzErrorCode: null, anlz: anlzWithBeats([]), vendor: 'rekordbox', effectiveSource: 'own' };
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), false);
});

test('an own terminal answer (missing or failed) is never patched over by the legacy fallback', () => {
	// /beatgrid-fallback reads the LEGACY librosa/MIK analysis row (own_%
	// rows are explicitly excluded server-side) but always labels its
	// response source: "own" regardless, so accepting it here would
	// silently replace an own lane's authoritative terminal answer with a
	// legacy grid dressed up as if own had produced it (Codex P1 BLOCKING,
	// PR #1587, second round).
	for (const status of ['missing', 'failed']) {
		const anlz = anlzWithBeats([]);
		anlz.beatgrid.source = 'own';
		anlz.beatgrid.status = status;
		const gate = { anlzErrorCode: null, anlz, vendor: 'local' };
		assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), false);
	}
});

test('an unknown vendor mapping is never guessed at', () => {
	// vendor null = /rb-meta has not answered yet. Guessing here would race a
	// real rekordbox grid.
	const gate = { anlzErrorCode: null, anlz: anlzWithBeats([]), vendor: null, effectiveSource: 'own' };
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), false);
});

test('a real ANLZ grid is always preferred, local track or not', () => {
	for (const vendor of ['local', 'rekordbox', null]) {
		const gate = { anlzErrorCode: null, anlz: anlzWithBeats(REAL_GRID), vendor, effectiveSource: 'own' };
		assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), false);
	}
});

test('no /anlz answer yet is not an empty grid', () => {
	const gate = { anlzErrorCode: null, anlz: null, vendor: 'local', effectiveSource: 'own' };
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), false);
});

test('a beat_count that disagrees with an empty beats[] still counts as no grid', () => {
	// beats[] is what every consumer paints and quantizes against, so it is
	// the emptiness that matters - a stale count must not lock out the fallback.
	const anlz = anlzWithBeats([]);
	anlz.beatgrid.beat_count = 412;
	const gate = { anlzErrorCode: null, anlz, vendor: 'local', effectiveSource: 'own' };
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), true);
});

test('"has a grid" means the same here as it does to quantize and Beat Sync', () => {
	// grid-features.hasRealBeatGrid is what effectiveQuantize / effectiveBeatSync
	// gate on. A one-beat grid fails it (the beat math needs an interval), so
	// the deck treats that track as gridless - and so must this lane, or a
	// local track would be refused the fallback while still having no quantize.
	const oneBeat = anlzWithBeats([{ n: 1, bpm: 127, t: 0.135 }]);
	assert.equal(gridFeatures.hasRealBeatGrid(oneBeat.beatgrid.beats), false);
	assert.equal(beatgridFallback.hasAnlzBeatgrid(oneBeat), false);
	assert.equal(
		beatgridFallback.shouldUseBeatgridFallback({
			anlzErrorCode: null,
			anlz: oneBeat,
			vendor: 'local',
			effectiveSource: 'own'
		}),
		true
	);
	// ... and a grid the beat math DOES accept is preferred, as always.
	const real = anlzWithBeats(REAL_GRID);
	assert.equal(gridFeatures.hasRealBeatGrid(real.beatgrid.beats), true);
	assert.equal(beatgridFallback.hasAnlzBeatgrid(real), true);
});

test('PARITY-02: rekordbox explicitly selected never gets an own-derived grid', () => {
	// Same two shapes as the two "fires" tests above, but with the daemon on
	// rekordbox: a DJ who chose rekordbox must not be quietly handed OWN's
	// grid just because rekordbox's own answer for this track is empty.
	const mappedGoneAnlz = { anlzErrorCode: 'ANALYSIS_NOT_FOUND', anlz: null, vendor: null, effectiveSource: 'rekordbox' };
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(mappedGoneAnlz), false);
	const unmappedEmpty = {
		anlzErrorCode: null,
		anlz: anlzWithBeats([]),
		vendor: 'local',
		effectiveSource: 'rekordbox'
	};
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(unmappedEmpty), false);
	const mappedNoPath = {
		anlzErrorCode: null,
		anlz: { ...anlzWithBeats([]), local_waveform: { status: 'decoded' } },
		vendor: 'rekordbox',
		effectiveSource: 'rekordbox'
	};
	assert.equal(
		beatgridFallback.shouldUseBeatgridFallback(mappedNoPath),
		true,
		'issue #2346: rekordbox-selected mapped-no-AnalysisDataPath may still reach apps.analysis'
	);
});

test('a rekordbox-mapped empty PQTZ without local_waveform never reaches the fallback', () => {
	const gate = {
		anlzErrorCode: null,
		anlz: anlzWithBeats([]),
		vendor: 'rekordbox',
		effectiveSource: 'rekordbox'
	};
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), false);
});

test('PARITY-02: an unconfirmed selection is treated the same as rekordbox, never guessed as own', () => {
	// undefined = the client has not yet polled the daemon for its current
	// selection. Mirrors the existing null-vendor "never guessed at" case.
	const gate = {
		anlzErrorCode: 'ANALYSIS_NOT_FOUND',
		anlz: null,
		vendor: null,
		effectiveSource: undefined
	};
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), false);
});

test('withFallbackBeatgrid swaps the grid, restamps the source, and returns a new object', () => {
	const anlz = anlzWithBeats([]);
	anlz.cues = [{ kind: 'memory', slot: null, in_ms: 900, out_ms: null, is_loop: false, active_loop: false, beat_loop_size: null, color_table_index: null, comment: null }];
	anlz.vocals = { status: 'demucs', segments: [] };
	// A deck's /anlz fetch can have landed while the server's effective source
	// was still 'rekordbox' - this stamp is what withFallbackBeatgrid must
	// overwrite, not just spread through, once it installs an own-derived grid.
	anlz.beatgrid_source = 'rekordbox';
	anlz.beatgrid_own_unavailable_reason = null;
	const fallback = {
		stable_id: 'abc123',
		source: 'apps.analysis',
		backend: 'librosa+madmom',
		backend_version: 'librosa==0.10.0',
		bpm: 127,
		bpm_confidence: 0.8,
		anlz_available: false,
		beatgrid: { beat_count: 2, beats: REAL_GRID }
	};

	const merged = beatgridFallback.withFallbackBeatgrid(anlz, fallback);

	assert.deepEqual(merged.beatgrid, fallback.beatgrid);
	assert.equal(merged.beatgrid_source, 'own');
	assert.equal(merged.beatgrid_own_unavailable_reason, null);
	// Everything the real payload DID carry survives untouched - the fallback
	// pipeline proposes no cues, no waveform and no vocals.
	assert.deepEqual(merged.cues, anlz.cues);
	assert.deepEqual(merged.waveform, anlz.waveform);
	assert.deepEqual(merged.vocals, { status: 'demucs', segments: [] });
	assert.equal(merged.stable_id, 'abc123');
	assert.equal(merged.points, 38400);
	// New object identity: the deck-snapshot beatgrid memo is keyed on it, so
	// mutating in place would serve a stale grid to every IPC consumer.
	assert.notEqual(merged, anlz);
	assert.deepEqual(anlz.beatgrid, { beat_count: 0, beats: [] });
	assert.equal(anlz.beatgrid_source, 'rekordbox');
});

test('withFallbackBeatgrid lands a trusted own grid when /beatgrid-fallback carries status ok', () => {
	const anlz = {
		...anlzWithBeats([]),
		local_waveform: { status: 'decoded' }
	};
	const fallback = {
		stable_id: 'abc123',
		source: 'apps.analysis',
		backend: 'librosa',
		backend_version: 'test',
		bpm: 128,
		bpm_confidence: 0.9,
		anlz_available: false,
		beatgrid: { source: 'own', status: 'ok', beat_count: 2, beats: REAL_GRID }
	};
	const merged = beatgridFallback.withFallbackBeatgrid(anlz, fallback);
	assert.equal(gridFeatures.hasTrustedBeatGrid(merged), true);
	assert.equal(
		gridFeatures.effectiveBeatSync({
			beat_sync_enabled: true,
			anlz: merged
		}),
		true
	);
});

test('withFallbackBeatgrid refuses to overwrite a real ANLZ grid', () => {
	const anlz = anlzWithBeats(REAL_GRID);
	const fallback = {
		stable_id: 'abc123',
		source: 'apps.analysis',
		backend: 'librosa+madmom',
		backend_version: 'librosa==0.10.0',
		bpm: 90,
		bpm_confidence: 0.4,
		anlz_available: true,
		beatgrid: { beat_count: 1, beats: [{ n: 1, bpm: 90, t: 5 }] }
	};
	assert.throws(
		() => beatgridFallback.withFallbackBeatgrid(anlz, fallback),
		/real ANLZ beatgrid/
	);
});

test('toSyntheticAnlzData carries the real beatgrid through, invents nothing else', () => {
	const fallback = {
		stable_id: 'abc123',
		source: 'apps.analysis',
		backend: 'librosa+madmom',
		backend_version: 'librosa==0.10.0',
		bpm: 127.5,
		bpm_confidence: 0.8,
		anlz_available: false,
		beatgrid: {
			beat_count: 2,
			beats: [
				{ n: 1, bpm: 127.5, t: 0.5 },
				{ n: 2, bpm: 127.5, t: 0.971 }
			]
		}
	};

	const synth = beatgridFallback.toSyntheticAnlzData(fallback);

	assert.equal(synth.stable_id, 'abc123');
	assert.deepEqual(synth.beatgrid, fallback.beatgrid);
	assert.deepEqual(synth.cues, []);
	assert.deepEqual(synth.phrases, []);
	assert.deepEqual(synth.vocals, { status: 'not_analyzed' });
	assert.equal(synth.waveform.kind, 'mono');
	assert.equal(synth.waveform.detail.length, 0);
	assert.deepEqual(synth.waveform.detail.low, []);
	assert.deepEqual(synth.waveform.preview.low, []);
});

test('a synthetic payload drives the same bars-to-grid-end countdown as a real one', () => {
	const fallback = {
		stable_id: 'abc123',
		source: 'apps.analysis',
		backend: 'librosa+madmom',
		backend_version: 'librosa==0.10.0',
		bpm: 120,
		bpm_confidence: 0.7,
		anlz_available: false,
		beatgrid: {
			beat_count: 8,
			beats: [
				{ n: 1, bpm: 120, t: 0 },
				{ n: 2, bpm: 120, t: 0.5 },
				{ n: 3, bpm: 120, t: 1.0 },
				{ n: 4, bpm: 120, t: 1.5 },
				{ n: 1, bpm: 120, t: 2.0 },
				{ n: 2, bpm: 120, t: 2.5 },
				{ n: 3, bpm: 120, t: 3.0 },
				{ n: 4, bpm: 120, t: 3.5 }
			]
		}
	};
	const synth = beatgridFallback.toSyntheticAnlzData(fallback);

	// No cues/phrases in the fallback payload, so the countdown target is
	// the end of the measured grid (wave-math.ts priority tier 3): 7 beats
	// from t=0 to the last beat at t=3.5 -> 1 full bar, and the 3 leftover
	// beats are floored away. The label is whole bars as of pin d56d98cd9c53
	// (Wed 2 Sep 2026); see tests/unit/bars-to-next-cue-label.test.mjs for why.
	assert.equal(waveMath.barsToNextCueLabel(synth, 0), '1Bars');
	// Past the end of the grid: nothing left to count down to.
	assert.equal(waveMath.barsToNextCueLabel(synth, 4000), null);
});

test('no beats at all (no downbeats measured) never invents a grid', () => {
	const fallback = {
		stable_id: 'abc123',
		source: 'apps.analysis',
		backend: 'librosa+madmom',
		backend_version: 'librosa==0.10.0',
		bpm: 0,
		bpm_confidence: 0,
		anlz_available: false,
		beatgrid: { beat_count: 0, beats: [] }
	};
	const synth = beatgridFallback.toSyntheticAnlzData(fallback);
	assert.equal(waveMath.barsToNextCueLabel(synth, 0), null);
});

test('sameBeatgrid treats extrapolated mark as part of grid identity', () => {
	const base = {
		source: 'own',
		beat_count: 2,
		beats: [
			{ n: 1, bpm: 128, t: 0.5, extrapolated: false },
			{ n: 2, bpm: 128, t: 0.969, extrapolated: true }
		]
	};
	const flipped = {
		...base,
		beats: [
			{ n: 1, bpm: 128, t: 0.5, extrapolated: false },
			{ n: 2, bpm: 128, t: 0.969, extrapolated: false }
		]
	};
	assert.equal(beatgridFallback.sameBeatgrid(base, base), true);
	assert.equal(beatgridFallback.sameBeatgrid(base, flipped), false);
});

test('withFallbackBeatgrid preserves extrapolated flags from the server payload', () => {
	const anlz = anlzWithBeats([]);
	const fallback = {
		stable_id: 'abc123',
		source: 'apps.analysis',
		backend: 'librosa+madmom',
		backend_version: 'librosa==0.10.0',
		bpm: 128,
		bpm_confidence: 0.8,
		anlz_available: false,
		beatgrid: {
			source: 'own',
			beat_count: 2,
			beats: [
				{ n: 1, bpm: 128, t: 0.5, extrapolated: false },
				{ n: 2, bpm: 128, t: 0.969, extrapolated: true }
			]
		}
	};
	const merged = beatgridFallback.withFallbackBeatgrid(anlz, fallback);
	assert.deepEqual(merged.beatgrid.beats, fallback.beatgrid.beats);
});

test('resolvePaintAnlz: real payload wins, fallback merges into a gridless one or stands alone', () => {
	const fallback = {
		stable_id: 'abc123',
		source: 'apps.analysis',
		backend: 'librosa+madmom',
		backend_version: 'librosa==0.10.0',
		bpm: 127,
		bpm_confidence: 0.8,
		anlz_available: false,
		beatgrid: { beat_count: 2, beats: REAL_GRID }
	};
	assert.equal(beatgridFallback.resolvePaintAnlz(null, null), null);
	assert.deepEqual(
		beatgridFallback.resolvePaintAnlz(null, fallback),
		beatgridFallback.toSyntheticAnlzData(fallback)
	);
	const gridless = anlzWithBeats([]);
	assert.equal(beatgridFallback.resolvePaintAnlz(gridless, null), gridless);
	const merged = beatgridFallback.resolvePaintAnlz(gridless, fallback);
	assert.deepEqual(merged.beatgrid, fallback.beatgrid);
	assert.equal(merged.beatgrid_source, 'own');
	// A stale ANALYSIS_NOT_FOUND entry beside a deck that holds a real grid:
	// the real grid wins and nothing throws (withFallbackBeatgrid would).
	const real = anlzWithBeats(REAL_GRID);
	assert.equal(beatgridFallback.resolvePaintAnlz(real, fallback), real);
});
