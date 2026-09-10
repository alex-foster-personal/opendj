import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
	MANIFEST_VERSION,
	buildManifest,
	manifestGate,
	manifestMismatches
} from '../live/fixture-manifest.mjs';

/**
 * Provenance for the live decode rung's inputs.
 *
 * The live runner reads real audio from a mutable, gitignored or
 * operator-supplied directory and cannot itself be run in CI (two browsers,
 * gigabytes of audio), so a defect in HOW it identifies its inputs would never
 * be caught by anything. That is the reason the comparison lives in its own
 * module and this file drives it directly.
 *
 * Regression lines:
 * - if a re-encode under the same name is accepted then two runs report the
 *   same PASS against different audio and the evidence means nothing
 * - if a rename with identical bytes is silently accepted then directory
 *   membership can drift without the record ever saying so
 * - if a malformed or empty recording compares as VERIFIED then the check
 *   reports the same zero whether it passed or could not run at all
 * - if a matching manifest reports a problem then the guard is unusable and
 *   the next operator deletes the file to go green
 * - if a run with no pinned manifest records one and carries on then it
 *   verified nothing, and on a fresh checkout that is every run
 * - if the recording mode can report acceptance success then the contract and
 *   the evidence for it were produced by the same unreviewed command
 */

const PARTS = ['vocals', 'drums', 'bass', 'other'];

function selected(overrides = {}) {
	return PARTS.map((part) => ({
		part,
		file: `/audio/${overrides[part]?.name ?? `${part}.flac`}`,
		bytes: Buffer.from(overrides[part]?.content ?? `${part}-audio`)
	}));
}

//-----------------------------------------------------------------------------

test('a manifest names its inputs by content, not by size and basename', () => {
	const manifest = buildManifest(selected());
	assert.equal(manifest.version, MANIFEST_VERSION);
	assert.deepEqual(
		manifest.parts.map((p) => p.part),
		PARTS,
		'recorded in part order, so a reader can line it up with the run'
	);
	assert.deepEqual(
		manifest.parts.map((p) => p.name),
		PARTS.map((part) => `${part}.flac`),
		'the basename is recorded because it is the half an operator can act on'
	);
	for (const entry of manifest.parts) {
		assert.match(entry.sha256, /^[0-9a-f]{64}$/, 'a full sha256, so a prefix cannot collide');
		assert.ok(entry.bytes > 0);
	}
	// Distinct inputs must produce distinct digests, or the whole record is
	// decorative: a constant would satisfy every comparison below.
	assert.equal(new Set(manifest.parts.map((p) => p.sha256)).size, PARTS.length);
});

test('an identical selection verifies, so the guard is usable rather than deleted', () => {
	// POSITIVE CONTROL for the empty result: it has to be reachable, or every
	// mismatch case below passes for the wrong reason.
	const recorded = buildManifest(selected());
	assert.deepEqual(manifestMismatches(recorded, buildManifest(selected())), []);
	// And through JSON, which is how it actually travels.
	assert.deepEqual(
		manifestMismatches(JSON.parse(JSON.stringify(recorded)), buildManifest(selected())),
		[]
	);
});

test('a re-encode under the same name is caught, which size alone cannot do', () => {
	const recorded = buildManifest(selected());
	// Same name, same byte count, different audio: exactly what the old
	// "basename plus MB" line reported as identical.
	const reencoded = buildManifest(selected({ drums: { content: 'drums-AUDIO' } }));
	assert.equal(recorded.parts[1].bytes, reencoded.parts[1].bytes, 'the size is unchanged');
	const problems = manifestMismatches(recorded, reencoded);
	assert.equal(problems.length, 1);
	assert.match(problems[0], /part drums is different audio/);
});

test('a rename with identical bytes is reported too, because membership drifted', () => {
	const recorded = buildManifest(selected());
	const renamed = buildManifest(selected({ bass: { name: 'aaa-bass.flac' } }));
	const problems = manifestMismatches(recorded, renamed);
	assert.equal(problems.length, 1);
	assert.match(problems[0], /same audio under a different name/);
});

test('a part that appears, disappears or changes shape is named', () => {
	const recorded = buildManifest(selected());

	const missing = { ...recorded, parts: recorded.parts.slice(0, 3) };
	assert.deepEqual(
		manifestMismatches(missing, buildManifest(selected())),
		['part other is decoded by this run but absent from the manifest']
	);

	const extra = {
		...recorded,
		parts: [...recorded.parts, { part: 'piano', name: 'p.flac', bytes: 1, sha256: 'ff' }]
	};
	assert.deepEqual(
		manifestMismatches(extra, buildManifest(selected())),
		['part piano is pinned by the manifest but was not decoded by this run']
	);

	const stale = { ...recorded, version: MANIFEST_VERSION - 1 };
	assert.match(manifestMismatches(stale, buildManifest(selected()))[0], /manifest version/);
});

test('a recording that pins nothing is a mismatch, never a quiet verification', () => {
	// THE failure this module exists to close. An unparseable file, an empty
	// object, a truncated write and a null all have nothing to compare, and a
	// comparison with nothing to do returns the same empty list a real pass
	// does. Each has to be loud instead.
	const observed = buildManifest(selected());
	for (const [label, recorded] of [
		['null', null],
		['a string from a failed parse', 'unparseable: SyntaxError'],
		['an array', []],
		['an empty object', {}],
		['a manifest with no parts array', { version: MANIFEST_VERSION }],
		['a manifest with an empty parts array', { version: MANIFEST_VERSION, parts: [] }]
	]) {
		const problems = manifestMismatches(recorded, observed);
		assert.ok(problems.length > 0, `${label} must not verify`);
	}
});

//-----------------------------------------------------------------------------
// The gate: what a run is ALLOWED to do with the manifest it just built.

test('a run with no pinned manifest is refused, never allowed to pin its own', () => {
	// THE hole in the first version of this: the gitignored default does not
	// exist in a fresh checkout, so whichever four files sorted first became
	// the contract and the next line was a PASS against them.
	const observed = buildManifest(selected());
	const gate = manifestGate({
		recordMode: false,
		manifestExists: false,
		recorded: null,
		observed
	});
	assert.equal(gate.action, 'missing');
});

test('recording is its own mode, and a recording run never reaches the checks', () => {
	const observed = buildManifest(selected());
	assert.equal(
		manifestGate({ recordMode: true, manifestExists: false, recorded: null, observed }).action,
		'record'
	);
	// And it cannot quietly re-pin: an existing manifest stops it, whether or
	// not the inputs drifted, so a directory that moved cannot become the new
	// contract as a side effect of a command someone ran for another reason.
	const pinned = buildManifest(selected());
	assert.equal(
		manifestGate({ recordMode: true, manifestExists: true, recorded: pinned, observed }).action,
		'refuse-overwrite'
	);
	const drifted = manifestGate({
		recordMode: true,
		manifestExists: true,
		recorded: pinned,
		observed: buildManifest(selected({ drums: { content: 'other-audio' } }))
	});
	assert.equal(drifted.action, 'refuse-overwrite');
	assert.equal(drifted.problems.length, 1, 'and it says what drifted, rather than only refusing');
});

test('only an ordinary run against a matching pinned manifest may continue', () => {
	// POSITIVE CONTROL for the whole gate: exactly one of its five outcomes
	// lets a browser open, and this is it. Without this the gate could refuse
	// everything and every case above would still pass.
	const pinned = buildManifest(selected());
	const gate = manifestGate({
		recordMode: false,
		manifestExists: true,
		recorded: pinned,
		observed: buildManifest(selected())
	});
	assert.deepEqual(gate, { action: 'verified', problems: [] });

	const changed = manifestGate({
		recordMode: false,
		manifestExists: true,
		recorded: pinned,
		observed: buildManifest(selected({ bass: { content: 'different' } }))
	});
	assert.equal(changed.action, 'mismatch');
	assert.equal(changed.problems.length, 1);

	// An unreadable pin is a pin that cannot be honored, not an absent one:
	// degrading it to `missing` would hand the next run a fresh baseline.
	const unreadable = manifestGate({
		recordMode: false,
		manifestExists: true,
		recorded: 'unparseable: SyntaxError',
		observed: buildManifest(selected())
	});
	assert.equal(unreadable.action, 'mismatch');
});

//-----------------------------------------------------------------------------
// The live run's lane oracle: the same margin the implementation applies.

test('the margin is read from the implementation, and unreadable means unreadable', async () => {
	const { laneMarginFrom, stopwatchLane } = await import('../live/lane-oracle.mjs');
	const real = readFileSync(
		new URL('../../src/lib/player/decode/flac-stem-decode.ts', import.meta.url),
		'utf8'
	);
	// POSITIVE CONTROL against the LIVE source, not a fixture: the point of
	// reading rather than copying is that the two cannot drift, and a test
	// against a hand-written string would drift with the copy.
	const margin = laneMarginFrom(real);
	assert.ok(margin !== null, 'the constant must be findable in the module it audits');
	assert.ok(margin > 1, `a margin of ${margin} would make the workers win by losing`);

	// If the constant is renamed or removed, this must report UNKNOWN rather
	// than a plausible default that silently audits against a stale number.
	assert.equal(laneMarginFrom(real.replace(/LANE_MARGIN/g, 'LANE_HEADROOM')), null);
	assert.equal(laneMarginFrom(''), null);
	assert.equal(laneMarginFrom('const LANE_MARGIN = 0.5;'), null, 'below 1 is a bad match');

	// The oracle itself: a win INSIDE the margin is a main-thread verdict,
	// which is the decision production takes on purpose and the live check
	// used to report as a disagreement and fail the run.
	assert.equal(stopwatchLane(1.2, margin), 'main-thread');
	assert.equal(stopwatchLane(margin, margin), 'main-thread', 'exactly the margin is not a win');
	assert.equal(stopwatchLane(1.71, margin), 'workers');
	assert.equal(stopwatchLane(0.38, margin), 'main-thread');
});
