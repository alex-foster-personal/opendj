import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

/** Issue #5609: library or grid span under 50% of decoded length. */

let math;

before(async () => {
	math = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
});

test('We-R-Superstars 36 s row on 180.51 s decode is flagged', () => {
	assert.equal(math.shortDurationFlag(36000, 36000, 180510), true);
});

test('170 s row on 180.51 s decode is not flagged', () => {
	assert.equal(math.shortDurationFlag(170000, 170000, 180510), false);
});

test('missing grid is not flagged as short', () => {
	assert.equal(math.shortDurationFlag(180510, null, 180510), false);
	assert.equal(math.shortDurationFlag(180510, undefined, 180510), false);
});

test('grid-only short with full library duration', () => {
	assert.equal(math.shortDurationFlag(180510, 36000, 180510), true);
});

test('library-only short with missing grid', () => {
	assert.equal(math.shortDurationFlag(36000, null, 180510), true);
});

test('exactly 50% is not flagged', () => {
	assert.equal(math.shortDurationFlag(90255, 90255, 180510), false);
});

test('non-finite decoded length is not flagged', () => {
	assert.equal(math.shortDurationFlag(36000, 36000, Number.NaN), false);
	assert.equal(math.shortDurationFlag(36000, 36000, Number.POSITIVE_INFINITY), false);
	assert.equal(math.shortDurationFlag(36000, 36000, 0), false);
	assert.equal(math.shortDurationFlag(36000, 36000, -1), false);
});

test('non-finite library or grid skips that arm', () => {
	assert.equal(math.shortDurationFlag(Number.NaN, 36000, 180510), true);
	assert.equal(math.shortDurationFlag(36000, Number.NaN, 180510), true);
	assert.equal(math.shortDurationFlag(Number.NaN, Number.NaN, 180510), false);
});

test('audio-engine warns on successful deck load only (#5609)', () => {
	const root = fileURLToPath(new URL('../..', import.meta.url));
	const engine = readFileSync(`${root}/src/lib/rb/audio-engine.svelte.ts`, 'utf8');
	assert.match(engine, /warnShortDurationOnLoad\(/);
	const failCall = engine.match(/recordDeckLoad\(\s*['"]deck-load-fail['"][\s\S]*?\);/);
	const failSwapCall = engine.match(/recordDeckLoad\(\s*['"]deck-load-fail-swap['"][\s\S]*?\);/);
	assert.ok(failCall, 'deck-load-fail recordDeckLoad call');
	assert.ok(failSwapCall, 'deck-load-fail-swap recordDeckLoad call');
	assert.doesNotMatch(failCall[0], /warnShortDurationOnLoad/);
	assert.doesNotMatch(failSwapCall[0], /warnShortDurationOnLoad/);
	const successBlock = engine.match(
		/recordDeckLoad\(`deck-load sid=\$\{stable_id\.slice\(0, 12\)\}`[\s\S]*?upgradeDeckBeatgrid/
	);
	assert.ok(successBlock, 'successful recordDeckLoad must precede upgradeDeckBeatgrid');
	assert.match(successBlock[0], /warnShortDurationOnLoad\(deck, candidateTrack, candidateAnlz, candidateBuffer\)/);
});

test('deck-load-context emits short-duration warn perf event', () => {
	const root = fileURLToPath(new URL('../..', import.meta.url));
	const source = readFileSync(`${root}/src/lib/rb/deck-load-context.ts`, 'utf8');
	const call = source.match(/recordPerfEvent\(\s*['"]short-duration['"][\s\S]*?\);/);
	assert.ok(call, 'recordPerfEvent must be called with short-duration');
	assert.match(call[0], /['"]warn['"]/);
	assert.doesNotMatch(call[0], /['"]error['"]/);
});
