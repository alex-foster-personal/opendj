/**
 * PLAY-08: the AutoPlay stall descriptor (issue #1640).
 *
 * The pure half. Given a terminal AutoPlay branch, this must produce something
 * an operator can act on hours later: what stopped, which tracks are involved,
 * how to get sound back.
 *
 * [if] the missing-audio branch is described [then] the headline names the
 *   count of unplayable tracks, not a generic "autoplay stopped" [⛔️ if the
 *   operator has to guess whether it was a library fault or a key/BPM dead end].
 * [if] more than STALL_TRACK_LIMIT tracks are blocked [then] the named list is
 *   capped but blocked_total is exact [⛔️ if a capped list reads as the whole
 *   remainder, which understates a library problem].
 * [if] a reason is added to the union without a headline [then] the exhaustive
 *   switch throws at build and at runtime [⛔️ if a new terminal branch renders
 *   as an empty banner].
 * [if] a caller omits the stalled source track [then] describe throws [⛔️ if a
 *   stall is recorded that the clear rule can never retire].
 * [if] the toast and the durable state describe the same stop [then] they name
 *   the SAME cause [⛔️ if the incident row in webui-client-errors-*.log names a
 *   different cause from the banner on screen - the log is what an audit reads
 *   months later].
 * [if] the same missing file occupies several playlist positions [then] it is
 *   named ONCE and counted once [⛔️ if the keyed render hits duplicate keys and
 *   the banner fails to draw the one thing it exists to show].
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/autoplay-stall.ts');
});

function row(stable_id, title = null, artist = null) {
	return { stable_id, key: '8A', bpm: 124, file_exists: false, title, artist };
}

// REQ: PLAY-13 / pin b91c8ba9b84b
describe('autoPlayStallDiagnosticMessage', () => {
	it('names silent idle arm failure for console and hunt collectors', () => {
		const line = mod.autoPlayStallDiagnosticMessage('no-deck-playing', null);
		assert.match(line, /^\[autoplay\] arm failed: no-deck-playing/);
		assert.match(line, /autoplay-stall\)$/);
	});
});

// REQ: PLAY-08
describe('describeAutoPlayStall', () => {
	it('names the cause and the count for a spent, unplayable playlist', () => {
		const stall = mod.describeAutoPlayStall({
			reason: 'missing-audio',
			source_stable_id: 'src-1',
			blocked: [row('a', 'Alpha', 'Ann'), row('b', 'Beta', 'Ben')]
		});
		assert.equal(stall.reason, 'missing-audio');
		assert.equal(stall.source_stable_id, 'src-1');
		assert.match(stall.headline, /all 2 remaining playlist tracks/);
		assert.match(stall.headline, /missing or stub audio/);
		assert.equal(stall.blocked_total, 2);
		assert.equal(stall.detail, null);
		assert.notEqual(stall.resume.length, 0, 'a stall with no way out is the bug, not the fix');
	});

	it('caps the named tracks but reports the exact total', () => {
		const blocked = Array.from({ length: mod.STALL_TRACK_LIMIT + 5 }, (_, i) =>
			row(`id-${i}`, `Track ${i}`, 'Someone')
		);
		const stall = mod.describeAutoPlayStall({
			reason: 'missing-audio',
			source_stable_id: 'src-1',
			blocked
		});
		assert.equal(stall.blocked.length, mod.STALL_TRACK_LIMIT);
		assert.equal(stall.blocked_total, mod.STALL_TRACK_LIMIT + 5);
	});

	it('carries the handoff error as detail for the handoff branches', () => {
		const stall = mod.describeAutoPlayStall({
			reason: 'handoff-incomplete',
			source_stable_id: 'src-1',
			blocked: [],
			detail: 'play refused: no_master'
		});
		assert.equal(stall.detail, 'play refused: no_master');
		assert.equal(stall.blocked.length, 0);
		assert.equal(stall.blocked_total, 0);
	});

	it('has a distinct headline and resume line for every reason', () => {
		const reasons = [
			'missing-audio',
			'no-next-in-order',
			'no-compatible-track',
			'candidates-failed-to-load',
			'handoff-attempts-exhausted',
			'handoff-incomplete',
			'master-handover-refused',
			'no-deck-playing'
		];
		const headlines = new Set();
		const resumes = new Set();
		for (const reason of reasons) {
			const stall = mod.describeAutoPlayStall({
				reason,
				source_stable_id: 'src-1',
				blocked: [row('a')]
			});
			headlines.add(stall.headline);
			resumes.add(stall.resume);
		}
		assert.equal(headlines.size, reasons.length, 'two branches sharing a headline is a mis-report');
		assert.equal(resumes.size, reasons.length, 'the way out differs per cause');
	});

	it('names a repeated missing file once, and counts distinct tracks', () => {
		const stall = mod.describeAutoPlayStall({
			reason: 'missing-audio',
			source_stable_id: 'src-1',
			// Playlist membership is keyed by position, so one file can occupy
			// several rows. Rendering that keyed by stable_id is a duplicate-key
			// error, i.e. the banner fails to draw at all.
			blocked: [row('a', 'Alpha', 'Ann'), row('a', 'Alpha', 'Ann'), row('b', 'Beta', 'Ben')]
		});
		assert.deepEqual(stall.blocked.map((t) => t.stable_id), ['a', 'b']);
		assert.equal(stall.blocked_total, 2, 'the operator relinks files, not positions');
		assert.equal(
			new Set(stall.blocked.map((t) => t.stable_id)).size,
			stall.blocked.length,
			'every rendered key must be unique'
		);
	});

	it('CONTROL: distinct tracks are all kept', () => {
		const stall = mod.describeAutoPlayStall({
			reason: 'missing-audio',
			source_stable_id: 'src-1',
			blocked: [row('a'), row('b'), row('c')]
		});
		assert.equal(stall.blocked_total, 3, 'de-duplication must not collapse different tracks');
	});

	it('says THREE DIFFERENT tracks failed, not one track three times', () => {
		// A candidate goes into the controller's `_claimedIds` before its
		// dispatch, so a retry can never pick the same id twice: the three
		// attempts are necessarily three different files. "the next track failed
		// three times" sent the operator hunting one flaky track that does not
		// exist. Mutating the wording back left every other test in this file
		// green, which is why this assertion is here at all.
		const stall = mod.describeAutoPlayStall({
			reason: 'handoff-attempts-exhausted',
			source_stable_id: 'src-1',
			blocked: [],
			detail: 'load refused'
		});
		assert.match(stall.headline, /three different candidate tracks/);
		assert.equal(
			/the next track/.test(stall.headline),
			false,
			'a singular headline mis-describes what the attempt budget actually counted'
		);
		assert.match(stall.resume, /unlikely to be one bad track/);
	});

	it('CONTROL: the single-candidate branch still reads as one track', () => {
		// handoff-incomplete IS about one specific track, so the plural above
		// must not have been applied by sweeping every headline.
		const stall = mod.describeAutoPlayStall({
			reason: 'handoff-incomplete',
			source_stable_id: 'src-1',
			blocked: [],
			detail: 'play refused'
		});
		assert.match(stall.headline, /the next track is on a deck/);
	});

	it('names the silent idle branch and tells the operator to press play', () => {
		const stall = mod.describeAutoPlayStall({
			reason: 'no-deck-playing',
			source_stable_id: 'src-1',
			blocked: []
		});
		assert.match(stall.headline, /no deck has been playing for 30 seconds/);
		assert.match(stall.resume, /Press play on a loaded deck/);
	});

	it('names the master-handover branch as a stop, and says how to resume it', () => {
		// The follower IS playing here; what failed is the master flag. Because
		// pickSourceDeck only ever arms off the master, nothing queues after
		// this track and the set ends when it does - which is the whole class
		// this state exists for, and it was only ever an expiring toast.
		const stall = mod.describeAutoPlayStall({
			reason: 'master-handover-refused',
			source_stable_id: 'next-1',
			blocked: [],
			detail: 'setDeckMaster refused'
		});
		assert.match(stall.headline, /could not be made master/);
		assert.match(stall.resume, /Press MASTER/);
		assert.equal(stall.detail, 'setDeckMaster refused');
	});

	it('refuses a reason it has no words for', () => {
		assert.throws(
			() =>
				mod.describeAutoPlayStall({
					reason: 'not-a-reason',
					source_stable_id: 'src-1',
					blocked: []
				}),
			/unhandled AutoPlay stall reason/
		);
	});

	it('refuses a stall with no source track, which could never be cleared', () => {
		assert.throws(
			() =>
				mod.describeAutoPlayStall({
					reason: 'handoff-incomplete',
					source_stable_id: '',
					blocked: []
				}),
			/stable_id of the stalled source/
		);
	});

	it('refuses a missing-audio stall with nothing blocked', () => {
		assert.throws(
			() =>
				mod.describeAutoPlayStall({
					reason: 'missing-audio',
					source_stable_id: 'src-1',
					blocked: []
				}),
			/at least one blocked track/
		);
	});
});

describe('describeStallTrack', () => {
	it('prefers artist and title, and falls back to the id rather than blank', () => {
		assert.equal(
			mod.describeStallTrack({ stable_id: 'x', title: 'Alpha', artist: 'Ann' }),
			'Ann - Alpha'
		);
		assert.equal(mod.describeStallTrack({ stable_id: 'x', title: 'Alpha', artist: null }), 'Alpha');
		assert.equal(mod.describeStallTrack({ stable_id: 'x', title: null, artist: null }), 'x');
		assert.equal(
			mod.describeStallTrack({ stable_id: 'x', title: null, artist: 'Ann' }),
			'Ann - x',
			'an artist with no title still beats an anonymous row'
		);
	});
});

describe('autoPlayStallReason / autoPlayExhaustionToast', () => {
	it('checks load failures before key and tempo', () => {
		assert.equal(
			mod.autoPlayStallReason({ all_missing: false, load_failures: true, enforce_order: false }),
			'candidates-failed-to-load'
		);
		assert.equal(
			mod.autoPlayStallReason({ all_missing: false, load_failures: true, enforce_order: true }),
			'candidates-failed-to-load',
			'a load failure is the cause whichever ordering mode is on'
		);
	});

	it('a load failure outranks an all-stub remainder', () => {
		// Both flags true: a compatible row failed to load AND everything left
		// unquarantined is a stub. The load failure is the more specific fault,
		// and the only one whose tracks the operator cannot see any other way -
		// a stub remainder is already visible in the library.
		assert.equal(
			mod.autoPlayStallReason({ all_missing: true, load_failures: true, enforce_order: false }),
			'candidates-failed-to-load'
		);
	});

	it('CONTROL: without load failures the pre-existing reasons are unchanged', () => {
		assert.equal(
			mod.autoPlayStallReason({ all_missing: true, load_failures: false, enforce_order: false }),
			'missing-audio'
		);
		assert.equal(
			mod.autoPlayStallReason({ all_missing: false, load_failures: false, enforce_order: true }),
			'no-next-in-order'
		);
		assert.equal(
			mod.autoPlayStallReason({ all_missing: false, load_failures: false, enforce_order: false }),
			'no-compatible-track'
		);
	});

	it('keeps the three shipped toast strings byte-identical', () => {
		// webui-client-errors-2026-09-09.log holds the first of these at
		// 18:23:39.614Z. That line is the incident anchor and a log grep that
		// matches it today must keep matching.
		assert.equal(
			mod.autoPlayExhaustionToast('missing-audio'),
			'auto-play: remaining playlist tracks are missing/stub audio'
		);
		assert.equal(
			mod.autoPlayExhaustionToast('no-next-in-order'),
			'auto-play: no next unplayed track in playlist order'
		);
		assert.equal(
			mod.autoPlayExhaustionToast('no-compatible-track'),
			'auto-play: no unplayed playlist track within key +-1 and Beat Sync BPM range'
		);
	});

	it('has its own string for the new reason, and refuses the handoff reasons', () => {
		assert.match(mod.autoPlayExhaustionToast('candidates-failed-to-load'), /failed to load/);
		assert.throws(
			() => mod.autoPlayExhaustionToast('handoff-incomplete'),
			/not an exhaustion toast/,
			'a handoff failure has no exhaustion toast; silently returning one would be a mis-report'
		);
		assert.throws(() => mod.autoPlayExhaustionToast('no-deck-playing'), /not an exhaustion toast/);
	});
});
