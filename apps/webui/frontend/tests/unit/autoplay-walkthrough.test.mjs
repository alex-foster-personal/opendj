import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let autoPlay;
let audioEngine;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/autoplay-walkthrough.ts');
	autoPlay = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
	audioEngine = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts');
});

function asTrackRow(row) {
	return { stable_id: row.id, key: row.key, bpm: row.bpm, file_exists: true };
}

describe('autoplay-walkthrough DEMO fixture', () => {
	it('greedy strands demo-5/demo-6 (real simulateAutoPlayChain replay)', () => {
		const { DEMO_ANCHOR, DEMO_ROWS } = mod;
		const { simulateAutoPlayChain, tempoBoundsFromPitchRange } = autoPlay;
		const bounds = tempoBoundsFromPitchRange(16);
		const chain = simulateAutoPlayChain({
			playlist: [DEMO_ANCHOR, ...DEMO_ROWS].map(asTrackRow),
			start_stable_id: DEMO_ANCHOR.id,
			enforce_play_order: false,
			maximize_reach: false,
			min_tempo_ratio: bounds.min,
			max_tempo_ratio: bounds.max
		});
		assert.deepEqual(chain, ['demo-anchor', 'demo-1', 'demo-2']);
	});

	it('reach reaches all four good rows (real simulateAutoPlayChain replay)', () => {
		const { DEMO_ANCHOR, DEMO_ROWS } = mod;
		const { simulateAutoPlayChain, tempoBoundsFromPitchRange } = autoPlay;
		const bounds = tempoBoundsFromPitchRange(16);
		const chain = simulateAutoPlayChain({
			playlist: [DEMO_ANCHOR, ...DEMO_ROWS].map(asTrackRow),
			start_stable_id: DEMO_ANCHOR.id,
			enforce_play_order: false,
			maximize_reach: true,
			min_tempo_ratio: bounds.min,
			max_tempo_ratio: bounds.max
		});
		assert.equal(chain.length, 5);
		assert.deepEqual(
			new Set(chain),
			new Set(['demo-anchor', 'demo-1', 'demo-2', 'demo-5', 'demo-6'])
		);
	});

	it('demo-3 rejects on key in both directions', () => {
		const { camelotKeysAreCompatible } = audioEngine;
		assert.equal(camelotKeysAreCompatible('2A', '8A'), false);
		assert.equal(camelotKeysAreCompatible('8A', '2A'), false);
	});

	it('demo-4 rejects on BPM against every row that ever becomes current', () => {
		const { bpmWithinPhaseLockRange, tempoBoundsFromPitchRange } = autoPlay;
		const bounds = tempoBoundsFromPitchRange(16);
		for (const bpm of [128, 124, 137, 145, 143]) {
			assert.equal(bpmWithinPhaseLockRange(220, bpm, bounds.min, bounds.max), false);
		}
	});

	it('isDemoRowCompatible agrees with the real predicates for every DEMO_ROWS entry', () => {
		const { DEMO_ROWS, isDemoRowCompatible } = mod;
		const { bpmWithinPhaseLockRange, tempoBoundsFromPitchRange } = autoPlay;
		const { camelotKeysAreCompatible } = audioEngine;
		const bounds = tempoBoundsFromPitchRange(16);
		for (const row of DEMO_ROWS) {
			const expected =
				camelotKeysAreCompatible(row.key, '8A') &&
				bpmWithinPhaseLockRange(row.bpm, 128, bounds.min, bounds.max);
			assert.equal(isDemoRowCompatible(row), expected, `mismatch for ${row.id}`);
		}
		const compatibleIds = DEMO_ROWS.filter(isDemoRowCompatible).map((r) => r.id);
		assert.deepEqual(compatibleIds, ['demo-1', 'demo-2', 'demo-5', 'demo-6']);
	});
});

describe('autoplay-walkthrough timelines', () => {
	it('reach timeline totals 9000ms', () => {
		const { buildWalkthroughTimeline, totalMs } = mod;
		assert.equal(totalMs(buildWalkthroughTimeline('reach')), 9000);
	});

	it('greedy timeline totals 7300ms', () => {
		const { buildWalkthroughTimeline, totalMs } = mod;
		assert.equal(totalMs(buildWalkthroughTimeline('greedy')), 7300);
	});

	it('enforce timeline totals 4100ms', () => {
		const { buildWalkthroughTimeline, totalMs } = mod;
		assert.equal(totalMs(buildWalkthroughTimeline('enforce')), 4100);
	});

	it('greedy contains no simulate phase and ends with 2 accepted rows', () => {
		const { buildWalkthroughTimeline } = mod;
		const frames = buildWalkthroughTimeline('greedy');
		assert.equal(
			frames.some((f) => f.phase === 'simulate'),
			false
		);
		const last = frames[frames.length - 1];
		assert.equal(last.phase, 'summary');
		assert.equal(last.acceptedIds.length, 2);
		assert.equal(last.strandedIds.length, 2);
	});

	it('reach ends with 4 accepted rows and 2 skipped rows, no stranding left', () => {
		const { buildWalkthroughTimeline } = mod;
		const frames = buildWalkthroughTimeline('reach');
		const last = frames[frames.length - 1];
		assert.equal(last.phase, 'summary');
		assert.equal(last.acceptedIds.length, 4);
		assert.equal(last.skippedIds.length, 2);
		assert.equal(last.strandedIds.length, 0);
	});

	it('enforce contains no reject/strand/simulate phase kinds and ends with all 6 accepted', () => {
		const { buildWalkthroughTimeline } = mod;
		const frames = buildWalkthroughTimeline('enforce');
		const kinds = new Set(frames.map((f) => f.phase));
		assert.equal(kinds.has('reject'), false);
		assert.equal(kinds.has('strand'), false);
		assert.equal(kinds.has('simulate'), false);
		for (const kind of kinds) {
			assert.ok(['anchor', 'handoff', 'summary'].includes(kind), `unexpected phase ${kind}`);
		}
		const last = frames[frames.length - 1];
		assert.equal(last.acceptedIds.length, 6);
	});

	it('frameAt at totalMs settles on the honest summary per mode (SA5 4.6)', () => {
		const { buildWalkthroughTimeline, frameAt, totalMs } = mod;

		const greedyFrames = buildWalkthroughTimeline('greedy');
		const greedyEnd = frameAt(greedyFrames, totalMs(greedyFrames));
		assert.equal(greedyEnd.frame.phase, 'summary');
		assert.equal(greedyEnd.done, true);
		assert.equal(greedyEnd.frame.strandedIds.length, 2);

		const reachFrames = buildWalkthroughTimeline('reach');
		const reachEnd = frameAt(reachFrames, totalMs(reachFrames));
		assert.equal(reachEnd.frame.phase, 'summary');
		assert.equal(reachEnd.frame.strandedIds.length, 0);

		const enforceFrames = buildWalkthroughTimeline('enforce');
		const enforceEnd = frameAt(enforceFrames, totalMs(enforceFrames));
		assert.equal(enforceEnd.frame.phase, 'summary');
		assert.equal(enforceEnd.frame.strandedIds.length, 0);
	});

	it('frameAt clamps below 0 to frame 0 and lands mid-frame / on-boundary correctly', () => {
		const { buildWalkthroughTimeline, frameAt } = mod;
		const frames = buildWalkthroughTimeline('enforce');

		const beforeStart = frameAt(frames, -500);
		assert.equal(beforeStart.frameIndex, 0);
		assert.equal(beforeStart.done, false);

		const midFirst = frameAt(frames, 400); // inside the 800ms anchor frame
		assert.equal(midFirst.frameIndex, 0);
		assert.equal(midFirst.done, false);

		const onBoundary = frameAt(frames, frames[0].holdMs); // exactly frame 0's end
		assert.equal(onBoundary.frameIndex, 1, 'a boundary lands on the next frame, not the previous one');

		const beyondEnd = frameAt(frames, 999_999);
		assert.equal(beyondEnd.frameIndex, frames.length - 1);
		assert.equal(beyondEnd.done, true);
	});
});

describe('autoplay-walkthrough rowTreatment precedence', () => {
	it('accepted beats every other treatment', () => {
		const { rowTreatment } = mod;
		const frame = {
			phase: 'reject',
			focusIds: ['x'],
			acceptedIds: ['x'],
			skippedIds: ['x'],
			strandedIds: ['x'],
			holdMs: 1,
			caption: ''
		};
		assert.equal(rowTreatment('x', frame), 'accepted');
	});

	it('skipped beats stranded and flash', () => {
		const { rowTreatment } = mod;
		const frame = {
			phase: 'reject',
			focusIds: ['x'],
			acceptedIds: [],
			skippedIds: ['x'],
			strandedIds: ['x'],
			holdMs: 1,
			caption: ''
		};
		assert.equal(rowTreatment('x', frame), 'skipped');
	});

	it('stranded beats flash', () => {
		const { rowTreatment } = mod;
		const frame = {
			phase: 'reject',
			focusIds: ['x'],
			acceptedIds: [],
			skippedIds: [],
			strandedIds: ['x'],
			holdMs: 1,
			caption: ''
		};
		assert.equal(rowTreatment('x', frame), 'stranded');
	});

	it('flash requires focus AND phase reject/simulate', () => {
		const { rowTreatment } = mod;
		const flashFrame = {
			phase: 'simulate',
			focusIds: ['x'],
			acceptedIds: [],
			skippedIds: [],
			strandedIds: [],
			holdMs: 1,
			caption: ''
		};
		assert.equal(rowTreatment('x', flashFrame), 'flash');

		const acceptFrame = { ...flashFrame, phase: 'accept' };
		assert.equal(rowTreatment('x', acceptFrame), 'neutral');
	});

	it('falls back to neutral when nothing matches', () => {
		const { rowTreatment } = mod;
		const frame = {
			phase: 'anchor',
			focusIds: [],
			acceptedIds: [],
			skippedIds: [],
			strandedIds: [],
			holdMs: 1,
			caption: ''
		};
		assert.equal(rowTreatment('x', frame), 'neutral');
	});
});

describe('autoplay-walkthrough camelotHue', () => {
	it('maps 8A to the expected hue and keeps compatible pairs close, incompatible pairs far', () => {
		const { camelotHue } = mod;
		assert.equal(camelotHue('8A'), ((8 - 1) / 12) * 360);
		assert.equal(Math.abs(camelotHue('8A') - camelotHue('9A')), 30);
		assert.equal(Math.abs(camelotHue('8A') - camelotHue('2A')), 180);
	});

	it('never returns null for any DEMO_ROWS key or the anchor key', () => {
		const { DEMO_ANCHOR, DEMO_ROWS, camelotHue } = mod;
		for (const row of [DEMO_ANCHOR, ...DEMO_ROWS]) {
			assert.notEqual(camelotHue(row.key), null, `${row.id} (${row.key}) hue should not be null`);
		}
	});

	it('returns null for an invalid Camelot string', () => {
		const { camelotHue } = mod;
		assert.equal(camelotHue('not-a-key'), null);
	});
});
