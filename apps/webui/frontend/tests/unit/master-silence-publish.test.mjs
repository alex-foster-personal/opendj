/**
 * The master meter must publish a READING, never a control value.
 *
 * ORIGIN (Codex, Thu 10 Sep 2026, on the fix for the Thu 10 Sep outage).
 * `noteMasterSilence` hands the fold `masterRms = 1` when there is no
 * analyser, so a graph that is mid-initialization or mid-teardown reads as
 * "not silence" and the dropout run resets. That sentinel is correct FOR THE
 * FOLD and is not a measurement, but it was also written straight into the
 * value the UI mirror publishes and stamped with `Date.now()`. The mirror
 * therefore reported `rms: 1, fresh: true` at exactly the moments no meter
 * existed: a full-scale synthetic number wearing the costume of a live
 * reading, which is the same misleading healthy-signal diagnosis that cost
 * most of a morning and that this whole change exists to remove.
 *
 * Regression lines:
 * - if the no-analyser sentinel is published as an rms then the mirror reports
 *   full-scale signal through a teardown
 * - if the no-analyser path is stamped fresh then an absent meter reads as a
 *   live one
 * - if NOTHING is ever published then the overshoot has blinded the mirror
 *   completely, which no report of the defect above would object to
 *
 * [if] the mirror publishes a meter reading nobody measured [then] fail, [else stop].
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { afterEach, before, describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SOURCE_PATH = fileURLToPath(
	new URL('../../src/lib/rb/master-silence-report.ts', import.meta.url)
);

describe('the master meter publishes only what it measured', () => {
	let report;
	let mirror;
	let source;
	before(async () => {
		report = await loadTypeScriptModule('src/lib/rb/master-silence-report.ts');
		mirror = await loadTypeScriptModule('src/lib/rb/audio-health-mirror.ts');
		source = await readFile(SOURCE_PATH, 'utf8');
	});

	it('reports no reading at all when there is no analyser', () => {
		// `null` is the REAL production argument for a graph without a meter -
		// `audio-engine.svelte.ts` passes `_masterAnalyser` straight through - so
		// this is the production path, not a stand-in for one.
		report.resetMasterSilenceWatch();
		report.noteMasterSilence(null, null, true, 1_000);
		const state = report.masterSilenceState();
		assert.equal(state.rms, null, 'the no-meter sentinel must never be published as an rms');
		assert.equal(state.at_ms, null, 'an unmeasured sample must not carry a measurement stamp');
	});

	it('leaves the fold alone: a missing meter is still not a dropout', () => {
		report.resetMasterSilenceWatch();
		for (let t = 0; t < 20_000; t += 100) report.noteMasterSilence(null, null, true, t);
		assert.equal(
			report.masterSilenceState().verdict,
			'ok',
			'a graph with no meter is not evidence of silence, so the run must keep resetting'
		);
	});

	it('makes the mirror say unknown rather than healthy', () => {
		report.resetMasterSilenceWatch();
		report.noteMasterSilence(null, null, true, 1_000);
		const state = report.masterSilenceState();
		const block = mirror.buildAudioHealthMirror({
			snapshot: null,
			rms: state.rms,
			rmsAgeMs: state.at_ms === null ? null : 5,
			silenceVerdict: state.verdict,
			events: [],
			nowMs: 1_000_000
		});
		assert.equal(block.meter.rms, null);
		assert.equal(block.meter.age_ms, null);
		assert.equal(block.meter.fresh, false, 'no meter is not a fresh meter');
	});

	it('still publishes a real sample, which is the overshoot control', () => {
		// A real AnalyserNode exists only in a browser, so the publishing half is
		// pinned against the production source itself rather than a stand-in
		// analyser manufactured here; the live path runs in the e2e audio soak.
		// Without this, "never publish" satisfies every line above and leaves the
		// mirror with no meter at all.
		const measured = source.slice(source.indexOf('if (analyser === null) {'));
		const elseBranch = measured.slice(measured.indexOf('} else {'), measured.indexOf('\t}\n'));
		assert.match(elseBranch, /_lastMasterRms = masterRms;/);
		assert.match(elseBranch, /_lastMasterRmsAtMs = Date\.now\(\);/);
	});

	it('overlays output-stalled-while-rendering when rms is high and output is stalled', () => {
		assert.match(source, /RENDERING_RMS_FLOOR = 0\.05/);
		assert.match(source, /audioOutputHealth\.snapshot\?\.browser\?\.verdict === 'stalled'/);
		assert.match(source, /output-stalled-while-rendering/);
	});
});

describe('the source PCM reader stays off the non-quiet master path', () => {
	let report;

	before(async () => {
		report = await loadTypeScriptModule('src/lib/rb/master-silence-report.ts');
	});

	afterEach(() => {
		report.resetMasterSilenceWatch();
		report.setSilenceSourceReader(null);
	});

	it('does not read deck buffers on the no-analyser path', () => {
		let readerCalls = 0;
		report.setSilenceSourceReader(() => {
			readerCalls += 1;
			return [];
		});
		report.resetMasterSilenceWatch();
		report.noteMasterSilence(null, null, true, 1_000);
		assert.equal(readerCalls, 0, 'no-meter sentinel must not trigger source PCM scans');
	});

	// A duck-typed analyser: `_masterRms` reads only fftSize and
	// getFloatTimeDomainData, so this drives the real noteMasterSilence wiring
	// (reader -> claimedLiveSourceIsSilent -> fold) end to end, which the fold
	// and pure-function tests alone cannot see.
	function analyserAt(level) {
		return {
			fftSize: 256,
			getFloatTimeDomainData(buf) {
				buf.fill(level);
			}
		};
	}

	function bufferFilled(level, seconds = 10, sampleRate = 44_100) {
		const data = new Float32Array(seconds * sampleRate).fill(level);
		return {
			sampleRate,
			length: data.length,
			numberOfChannels: 1,
			getChannelData() {
				return data;
			}
		};
	}

	function holdQuietMaster(readSnaps, masterLevel = 0) {
		report.resetMasterSilenceWatch();
		const analyser = analyserAt(masterLevel);
		let reported = false;
		let readerCalls = 0;
		report.setSilenceSourceReader(() => {
			readerCalls += 1;
			return readSnaps();
		});
		for (let t = 0; t <= 3_000; t += 100) {
			report.noteMasterSilence(analyser, null, true, t);
			if (report.masterSilenceState().verdict === 'silent-while-playing') reported = true;
		}
		return { reported, readerCalls };
	}

	it('silent source under a quiet master is content, not a dropout (AC1, AC4)', () => {
		const buffer = bufferFilled(0);
		const { reported, readerCalls } = holdQuietMaster(() => [
			{ claims_live: true, buffer, position_sec: 2 }
		]);
		assert.ok(readerCalls > 0, 'the quiet-master path must consult the source reader');
		assert.equal(reported, false);
	});

	it('loud source under a quiet master still reports (AC3, the overshoot control)', () => {
		const buffer = bufferFilled(0.5);
		const { reported } = holdQuietMaster(() => [{ claims_live: true, buffer, position_sec: 2 }]);
		assert.equal(reported, true, 'a real dropout must still reach silent-while-playing');
	});

	it('does not read deck buffers while the master carries signal', () => {
		const buffer = bufferFilled(0);
		const { reported, readerCalls } = holdQuietMaster(
			() => [{ claims_live: true, buffer, position_sec: 2 }],
			0.5
		);
		assert.equal(readerCalls, 0, 'the source scan must stay off the loud-master path');
		assert.equal(reported, false);
	});
});
