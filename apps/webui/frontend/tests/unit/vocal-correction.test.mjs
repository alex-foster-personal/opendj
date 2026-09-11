/**
 * Vocal-area hit-test and `vocal-area:` anchor codec (FB-12 / issue #698).
 *
 * Regression lines:
 * - if a time inside [start_s, end_s) does not return that region then broken
 * - if a time on end_s is claimed by that region then broken (half-open)
 * - if not_analyzed / empty demucs / no_vocals invents a region then broken
 * - if encode/parse drop stable_id, bounds, or status then broken
 * - if parseVocalAnchor('#foo') is non-null then broken (must not swallow FB-03 CSS anchors)
 * - if wavestack mapping disagrees with waveClickTargetMs at the window center then broken
 * - if strip mapping at x=0 / x=width is not 0 / duration then broken
 * - if default text contains an U+2014 character or omits stable_id then broken
 */

import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STABLE_ID = 'b21e3436628ac518dd8a219b26d301629d82256c';

let vocal;
let scrub;

before(async () => {
	vocal = await loadTypeScriptModule('src/lib/rb/vocal-correction.ts');
	scrub = await loadTypeScriptModule('src/lib/components/rb/wave/wave-scrub.ts');
});

const region = { start_s: 12.3, end_s: 45.6, intensity: 3 };

function rekordboxVocals(regions) {
	return { status: 'rekordbox', fps: 75, regions };
}

function demucsVocals(regions) {
	return { status: 'demucs', fps: 10, regions };
}

test('regionAtTime returns the region for a time inside [start_s, end_s)', () => {
	const hit = vocal.regionAtTime([region], 12.3);
	assert.deepEqual(hit, region);
	assert.deepEqual(vocal.regionAtTime([region], 30), region);
	assert.deepEqual(vocal.regionAtTime([region], 45.599), region);
});

test('regionAtTime is half-open: a time on end_s is not claimed by that region', () => {
	assert.equal(vocal.regionAtTime([region], 45.6), null);
	assert.equal(vocal.regionAtTime([region], 12.299), null);
});

test('not_analyzed / empty demucs / no_vocals never invent a region', () => {
	const atNotAnalyzed = vocal.hitFromVocals({
		stableId: STABLE_ID,
		vocals: { status: 'not_analyzed' },
		timeS: 20
	});
	assert.equal(atNotAnalyzed.kind, 'at');
	assert.equal(atNotAnalyzed.time_s, 20);
	assert.equal(atNotAnalyzed.status, 'not_analyzed');

	const atEmptyDemucs = vocal.hitFromVocals({
		stableId: STABLE_ID,
		vocals: demucsVocals([]),
		timeS: 20
	});
	assert.equal(atEmptyDemucs.kind, 'at');
	assert.equal(atEmptyDemucs.status, 'demucs');

	const atNoVocals = vocal.hitFromVocals({
		stableId: STABLE_ID,
		vocals: { status: 'no_vocals', fps: 75, regions: [] },
		timeS: 20
	});
	assert.equal(atNoVocals.kind, 'at');
	assert.equal(atNoVocals.status, 'no_vocals');
});

test('hitFromVocals names the region when the click time falls inside one', () => {
	const hit = vocal.hitFromVocals({
		stableId: STABLE_ID,
		vocals: rekordboxVocals([region]),
		timeS: 20
	});
	assert.equal(hit.kind, 'region');
	assert.equal(hit.start_s, 12.3);
	assert.equal(hit.end_s, 45.6);
	assert.equal(hit.intensity, 3);
	assert.equal(hit.status, 'rekordbox');
	assert.equal(hit.stable_id, STABLE_ID);
});

test('encode/parse keep stable_id, bounds, and status', () => {
	const regionHit = vocal.hitFromVocals({
		stableId: STABLE_ID,
		vocals: demucsVocals([region]),
		timeS: 20
	});
	const encodedRegion = vocal.encodeVocalAnchor(regionHit);
	assert.equal(
		encodedRegion,
		`vocal-area:${STABLE_ID}:12.30-45.60:demucs`
	);
	const parsedRegion = vocal.parseVocalAnchor(encodedRegion);
	assert.equal(parsedRegion.stable_id, STABLE_ID);
	assert.equal(parsedRegion.kind, 'region');
	assert.equal(parsedRegion.start_s, 12.3);
	assert.equal(parsedRegion.end_s, 45.6);
	assert.equal(parsedRegion.status, 'demucs');
	assert.equal(vocal.encodeVocalAnchor(parsedRegion), encodedRegion);

	const atHit = vocal.hitFromVocals({
		stableId: STABLE_ID,
		vocals: { status: 'not_analyzed' },
		timeS: 67.2
	});
	const encodedAt = vocal.encodeVocalAnchor(atHit);
	assert.equal(encodedAt, `vocal-area:${STABLE_ID}@67.20:not_analyzed`);
	const parsedAt = vocal.parseVocalAnchor(encodedAt);
	assert.deepEqual(parsedAt, atHit);
});

test("parseVocalAnchor('#foo') is null so FB-03 CSS anchors stay CSS anchors", () => {
	assert.equal(vocal.parseVocalAnchor('#foo'), null);
	assert.equal(vocal.parseVocalAnchor('[data-testid="waveform-seek-deck-1"]'), null);
	assert.equal(vocal.parseVocalAnchor('.rb-waverow'), null);
	assert.equal(vocal.parseVocalAnchor(''), null);
});

test('wavestack mapping agrees with waveClickTargetMs at the window center', () => {
	const args = {
		pointerX: 200,
		widthPx: 400,
		centerPositionMs: 60_000,
		durationMs: 180_000,
		pitch: 1
	};
	const seconds = vocal.waveRowTimeS(args);
	const ms = scrub.waveClickTargetMs({
		centerPositionMs: args.centerPositionMs,
		pointerX: args.pointerX,
		widthPx: args.widthPx,
		durationMs: args.durationMs,
		windowSeconds: 24 * args.pitch
	});
	assert.equal(seconds, ms / 1000);
	assert.equal(seconds, 60);
});

test('strip mapping at x=0 / x=width is 0 / duration', () => {
	assert.equal(
		vocal.stripTimeS({ pointerX: 0, widthPx: 400, durationMs: 180_000 }),
		0
	);
	assert.equal(
		vocal.stripTimeS({ pointerX: 400, widthPx: 400, durationMs: 180_000 }),
		180
	);
});

test('default text names stable_id and never uses an U+2014 or U+2013 characters', () => {
	const regionHit = vocal.hitFromVocals({
		stableId: STABLE_ID,
		vocals: demucsVocals([region]),
		timeS: 20
	});
	const regionText = vocal.formatVocalComment(regionHit);
	assert.equal(
		regionText,
		`Incorrect vocal area 12.30s-45.60s (demucs) on ${STABLE_ID}`
	);
	assert.ok(!regionText.includes('\u2014') && !regionText.includes('\u2013'));
	assert.ok(regionText.includes(STABLE_ID));

	const atHit = vocal.hitFromVocals({
		stableId: STABLE_ID,
		vocals: { status: 'not_analyzed' },
		timeS: 67.2
	});
	const atText = vocal.formatVocalComment(atHit);
	assert.equal(
		atText,
		`Incorrect vocal area at 67.20s (not_analyzed) on ${STABLE_ID}`
	);
	assert.ok(!atText.includes('\u2014') && !atText.includes('\u2013'));
	assert.ok(atText.includes(STABLE_ID));
});
