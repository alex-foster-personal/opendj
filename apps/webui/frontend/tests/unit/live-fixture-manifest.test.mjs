import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	MANIFEST_VERSION,
	buildManifest,
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
