/**
 * LIBM-184 / issue #3934 - the All Tracks badge counts tracks with audio on
 * this machine, the same predicate as the playlist badges under it.
 *
 * [if] the summary carries availability buckets [then] the badge is `present`
 *   and the title names streaming, off-machine, unmounted, broken and pathless
 * [if] availability is unmeasured (null or 'unknown') [then] the badge keeps the
 *   non-broken count and the title says audio here was not measured
 * [if] availability is malformed (missing, a bad bucket, or buckets that do not
 *   sum to total) [then] the badge is an error state: no count, the title names why
 * [if] present is 0 [then] the badge reads 0, not the non-broken fallback
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { allTracksBadge } = await loadTypeScriptModule(
	'src/lib/components/rb/browser/all-tracks-badge.ts'
);

// The shape of Issue #3934's packaged-app library: 1355 non-broken by the old
// rule, of which 315 streaming and 1 pathless.
const AVAILABILITY = {
	total: 1365,
	present: 1029,
	broken_here: 10,
	off_machine: 10,
	awaiting_volume: 0,
	streaming: 315,
	pathless: 1
};

test('a valid availability answer shows present, not total minus broken', () => {
	const badge = allTracksBadge(1355, 10, AVAILABILITY);
	assert.equal(badge.count, 1029);
	assert.equal(
		badge.title,
		'1029 tracks with audio on this machine. Not counted: 315 streaming, ' +
			'10 on other machines, 0 on unmounted drives, 10 with a broken link here, 1 with no file path'
	);
	assert.doesNotMatch(badge.title, /playable/);
});

test('present 0 is a value, not a reason to fall back (overshoot control)', () => {
	const badge = allTracksBadge(40, 0, {
		...AVAILABILITY,
		present: 0,
		total: AVAILABILITY.total - AVAILABILITY.present
	});
	assert.equal(badge.count, 0);
	assert.match(badge.title, /^0 tracks with audio on this machine/);
});

test('unknown availability keeps the non-broken count and says so', () => {
	for (const availability of ['unknown', null]) {
		const badge = allTracksBadge(1355, 10, availability);
		assert.equal(badge.count, 1355);
		assert.equal(
			badge.title,
			'1355 non-broken tracks, 10 broken (audio on this machine not measured)'
		);
	}
});

/** A malformed answer is an error state: no count, and the title names the defect. */
function assertMalformed(badge, reason) {
	assert.equal(badge.count, null, 'a malformed answer never shows a plausible count');
	assert.match(badge.error, /^availability answer is malformed \(/);
	assert.match(badge.error, reason);
	assert.equal(badge.title, `track count unavailable: ${badge.error}`);
}

test('a malformed bucket is an error, not half-read and not the old count (negative control)', () => {
	const { streaming: _drop, ...missing } = AVAILABILITY;
	assertMalformed(allTracksBadge(1355, 10, missing), /bucket streaming is missing/);
	assertMalformed(
		allTracksBadge(1355, 10, { ...AVAILABILITY, present: -1 }),
		/bucket present is -1/
	);
	assertMalformed(
		allTracksBadge(1355, 10, { ...AVAILABILITY, present: 1.5 }),
		/bucket present is 1\.5/
	);
	assertMalformed(
		allTracksBadge(1355, 10, { ...AVAILABILITY, present: '1029' }),
		/bucket present is "1029"/
	);
	assertMalformed(allTracksBadge(1355, 10, 42), /expected bucket counts, got number/);
	assertMalformed(allTracksBadge(1355, 10, 'gone'), /expected bucket counts, got string/);
	assertMalformed(allTracksBadge(1355, 10, []), /expected bucket counts, got an array/);
	// A missing field or an unwired prop is contract drift, not "not measured" (Sol P1, e52910aef9).
	assertMalformed(allTracksBadge(1355, 10, undefined), /expected bucket counts, got undefined/);
});

test('buckets that do not sum to total are an error, not a measurement (negative control)', () => {
	assertMalformed(
		allTracksBadge(1355, 10, { ...AVAILABILITY, total: AVAILABILITY.total + 1 }),
		/buckets sum to 1365, not the total 1366/
	);
	assertMalformed(
		allTracksBadge(1355, 10, { ...AVAILABILITY, present: AVAILABILITY.present - 1 }),
		/sum to 1364/
	);
	assertMalformed(
		allTracksBadge(1355, 10, { ...AVAILABILITY, streaming: AVAILABILITY.streaming + 5 }),
		/sum to 1370/
	);
	// Control: the exact partition is still read as a measurement, with no error.
	const ok = allTracksBadge(1355, 10, AVAILABILITY);
	assert.equal(ok.count, AVAILABILITY.present);
	assert.equal(ok.error, null);
});

test('only an unmeasured answer falls back to the non-broken count (overshoot control)', () => {
	for (const availability of ['unknown', null]) {
		assert.equal(allTracksBadge(1355, 10, availability).error, null, String(availability));
	}
});

test('counts still loading render as unknown, never zero', () => {
	const badge = allTracksBadge(null, null, 'unknown');
	assert.equal(badge.count, null);
	assert.equal(badge.title, 'loading non-broken and broken track counts');
});
