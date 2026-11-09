/**
 * SYNCLOG-9650 / SYNCLOG-9650b: Beat Sync OFF -> ON re-snaps tempo AND the 4-beat bar, and every
 * out-of-sync moment leaves a greppable SYNC_DRIFT / SYNC_RESNAP log line.
 *
 * the maintainer, Wed 4 Nov 2026: "beat sync when turned off and on isn't resnapping the
 * four beats properly". Live on silver (build f51bb5f9): decks 3 and 4 both
 * playing with Beat Sync lit, deck 3 at 130.02 BPM and deck 4 at 132.10, and
 * nothing in any log said so. The re-enable press returned silently when the
 * pressed deck was the master and when the master's grid was untrusted.
 *
 * [if] a re-enabled deck is the playing master [then] its playing followers are
 *   re-snapped to it [⛔️ if the press is a silent no-op]
 * [if] the master's grid is untrusted [then] the press says so and logs
 *   SYNC_RESNAP outcome=skipped [⛔️ if it returns silently]
 * [if] the follower's downbeat sits on the master's beat 2 [then] the measure
 *   reports bar_slip_beats=1 and BAR mode calls it drifting [⛔️ if beat phase
 *   alone (0 ms) reads as in sync]
 * [if] drift holds past the confirm window [then] one SYNC_DRIFT state=start
 *   line, one per relog window while it lasts, and state=end when it clears
 *   [⛔️ if jitter shorter than the window logs, or a long drift logs every tick]
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter, readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/** A constant grid: `count` beats at `bpm` from `firstSec`, bar numbers from `firstN`. */
function grid(bpm, count, firstSec = 0.1, firstN = 1) {
	const period = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({
		n: ((firstN - 1 + i) % 4) + 1,
		bpm,
		t: Number((firstSec + i * period).toFixed(5))
	}));
}

let drift;
before(async () => {
	drift = await loadTypeScriptModule('src/lib/rb/sync-drift.ts');
});

test('[SYNCLOG-9650b] an in-phase, in-bar follower measures 0 ms and 0 bar slip', () => {
	const beats = grid(128, 200);
	const at = beats[40].t + 0.1;
	const m = drift.measureSyncDrift(
		{ beats, positionSec: at, tempo: 1 },
		{ beats, positionSec: at, tempo: 1 },
		{ barPhaseTrusted: true }
	);
	assert.ok(
		Math.abs(m.driftMs) < 1e-6,
		`if driftMs is ${m.driftMs} then an identical deck reads as drifting`
	);
	assert.equal(m.barSlipBeats, 0);
	assert.equal(drift.isSyncDrifting(m, 'bar'), false);
	assert.equal(m.masterBpm.toFixed(2), '128.00');
});

test('[SYNCLOG-9650b] a whole-beat bar slip is drift in BAR mode although beat phase is 0 ms', () => {
	const beats = grid(128, 200);
	const master = { beats, positionSec: beats[40].t + 0.05, tempo: 1 }; // beats[40].n === 1
	const follower = { beats, positionSec: beats[41].t + 0.05, tempo: 1 }; // n === 2, same fraction
	const m = drift.measureSyncDrift(master, follower, { barPhaseTrusted: true });
	assert.ok(Math.abs(m.driftMs) < 1e-6, 'beat phase is identical');
	assert.equal(
		m.barSlipBeats,
		1,
		'if bar slip is not 1 then a downbeat on the master beat 2 is invisible'
	);
	assert.equal(
		drift.isSyncDrifting(m, 'bar'),
		true,
		'if BAR reads this as in sync then re-enable never had a target'
	);
	assert.equal(drift.isSyncDrifting(m, 'beat'), false, 'BEAT mode only cares about the beat');
	assert.equal(m.masterBarPhase.toFixed(2), '1.11');
	assert.equal(m.followerBarPhase.toFixed(2), '2.11');
	const behind = drift.measureSyncDrift(
		master,
		{ ...follower, positionSec: beats[39].t + 0.05 },
		{ barPhaseTrusted: true }
	);
	assert.equal(behind.barSlipBeats, -1);
});

test('[SYNCLOG-9650b] untrusted bars or a folded tempo report no bar slip', () => {
	const beats = grid(128, 200);
	const master = { beats, positionSec: beats[40].t, tempo: 1 };
	const follower = { beats, positionSec: beats[42].t, tempo: 1 };
	assert.equal(
		drift.measureSyncDrift(master, follower, { barPhaseTrusted: false }).barSlipBeats,
		null
	);
	assert.equal(
		drift.measureSyncDrift(master, follower, { barPhaseTrusted: true, normalization: 2 })
			.barSlipBeats,
		null
	);
});

test('[SYNCLOG-9650b] the live silver pair: 130.02 against 132.10 BPM drifts by the ms the tempos imply', () => {
	// Deck 4 Sad Clown (own grid 132.1, n starts at 2) as master at tempo 1;
	// deck 3 Milkshake (rekordbox 118) stuck at the old lock's 1.10188.
	const master = grid(132.1, 400, 0.48579, 2);
	const follower = grid(118, 300, 0.074, 1);
	const followerTempo = 1.1018767455643723;
	const m0 = drift.measureSyncDrift(
		{ beats: master, positionSec: master[100].t, tempo: 1 },
		{ beats: follower, positionSec: follower[100].t, tempo: followerTempo },
		{ barPhaseTrusted: true }
	);
	assert.equal(m0.followerBpm.toFixed(2), '130.02');
	assert.equal(m0.masterBpm.toFixed(2), '132.10');
	// 10 wall seconds later the follower has fallen behind by 10 * (1 - 130.02/132.1) beats.
	const m1 = drift.measureSyncDrift(
		{ beats: master, positionSec: master[100].t + 10, tempo: 1 },
		{ beats: follower, positionSec: follower[100].t + 10 * followerTempo, tempo: followerTempo },
		{ barPhaseTrusted: true }
	);
	const expectedBeats = -10 * (1 - 130.0209813230469 / 132.1) * (132.1 / 60);
	const delta = m1.driftBeats - m0.driftBeats;
	assert.ok(
		Math.abs(delta - expectedBeats) < 0.01,
		`drift moved ${delta} beats, expected ${expectedBeats}`
	);
	assert.ok(
		drift.isSyncDrifting(m1, 'beat'),
		'if a third of a beat apart reads in sync the flag never fires'
	);
});

test('[SYNCLOG-9650b] off-grid positions measure null rather than a fake phase', () => {
	const beats = grid(128, 20);
	assert.equal(
		drift.measureSyncDrift(
			{ beats, positionSec: 0, tempo: 1 },
			{ beats, positionSec: beats[5].t, tempo: 1 },
			{ barPhaseTrusted: true }
		),
		null
	);
	assert.throws(() =>
		drift.measureSyncDrift(
			{ beats, positionSec: 1, tempo: 0 },
			{ beats, positionSec: 1, tempo: 1 },
			{ barPhaseTrusted: true }
		)
	);
});

test('[SYNCLOG-9650b] drift episodes: confirm, relog, end; jitter never logs', () => {
	const mon = drift.createSyncDriftMonitor();
	const { SYNC_DRIFT_CONFIRM_MS: confirm, SYNC_DRIFT_RELOG_MS: relog } = drift;
	assert.equal(mon.observe(3, true, 0), null);
	assert.equal(
		mon.observe(3, false, confirm - 1),
		null,
		'if a blip shorter than the confirm window ends with a line then jitter logs'
	);
	assert.equal(mon.observe(3, true, 1000), null);
	assert.equal(mon.observe(3, true, 1000 + confirm), 'start');
	assert.equal(
		mon.observe(3, true, 1000 + confirm + 250),
		null,
		'if every tick logs then the log floods'
	);
	assert.equal(mon.observe(3, true, 1000 + confirm + relog), 'ongoing');
	assert.equal(mon.observe(4, true, 1000 + confirm + relog), null, 'decks keep separate episodes');
	assert.equal(mon.observe(3, false, 1000 + confirm + relog + 1), 'end');
	assert.equal(mon.observe(3, false, 1000 + confirm + relog + 2), null);
	mon.observe(4, true, 0);
	mon.forget(4);
	assert.equal(mon.observe(4, false, 99_999), null, 'a forgotten deck writes no end line');
});

test('[SYNCLOG-9650] re-enable policy: master resnaps followers, untrusted master is a loud skip', () => {
	const act = (deck, playingMaster, masterGridTrusted = true) =>
		drift.beatSyncReenableAction({ deck, playingMaster, masterGridTrusted });
	assert.deepEqual(act(2, 1), { kind: 'join-master', master: 1 });
	assert.deepEqual(
		act(1, 1),
		{ kind: 'resnap-followers' },
		'if the master press is a no-op then the group never re-snaps'
	);
	assert.deepEqual(act(3, 4, false), { kind: 'skip', reason: 'master_grid_untrusted' });
	assert.deepEqual(
		act(4, 4, false),
		{ kind: 'skip', reason: 'master_grid_untrusted' },
		'if an untrusted master re-snaps its followers then the PLAY-28 unsynced mix enters the grid path'
	);
	assert.deepEqual(act(3, null), { kind: 'skip', reason: 'no_playing_master' });
});

test('[SYNCLOG-9650b] log lines start with the tag and carry greppable key=value fields', () => {
	const line = drift.formatSyncLogLine(drift.SYNC_DRIFT_TAG, {
		state: 'start',
		follower: 3,
		master: 4,
		...drift.syncDriftFields({
			driftMs: -41.25,
			driftBeats: -0.0911,
			barSlipBeats: 0,
			masterBarPhase: 2.314,
			followerBarPhase: 2.223,
			masterBpm: 132.1,
			followerBpm: 130.0209
		}),
		follower_track: 'abc',
		locked: false
	});
	assert.equal(
		line,
		'SYNC_DRIFT state=start follower=3 master=4 drift_ms=-41.3 drift_beats=-0.091 bar_slip_beats=0 ' +
			'master_bar_phase=2.31 follower_bar_phase=2.22 master_bpm=132.10 follower_bpm=130.02 follower_track=abc locked=false'
	);
	assert.match(
		drift.formatSyncLogLine(drift.SYNC_RESNAP_TAG, {
			drift_ms: drift.syncDriftFields({
				driftMs: 0,
				driftBeats: 0,
				barSlipBeats: null,
				masterBarPhase: 1,
				followerBarPhase: 1,
				masterBpm: 1,
				followerBpm: 1
			}).drift_ms
		}),
		/^SYNC_RESNAP drift_ms=\+0\.0$/
	);
	assert.throws(() => drift.formatSyncLogLine(drift.SYNC_RESNAP_TAG, { reason: 'two words' }));
});

test('[SYNCLOG-9650] the Web Audio setBeatSync enable path takes the re-enable policy and logs it', () => {
	const body = engineBlockAfter('setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {');
	assert.match(
		body,
		/beatSyncReenableAction\(/,
		'if setBeatSync does not ask the policy then the master press stays a no-op'
	);
	assert.match(
		body,
		/_resnapFollowersOfMaster\(deck\)/,
		'the master branch re-snaps its followers'
	);
	assert.match(body, /master_grid_untrusted/, 'the untrusted-master skip is logged by name');
});

test('[SYNCLOG-9650] the Web Audio presentation tick checks drift, and the Rust re-enable resnaps followers', () => {
	const tick = engineBlockAfter('function _tick(): void {');
	assert.match(
		tick,
		/_checkSyncDrift\(/,
		'if the tick never measures drift then SYNC_DRIFT never fires'
	);
	const rust = readFrontendSource('src/lib/audio-engine/rust-transport.ts');
	const setBeatSyncAt = rust.indexOf('async function _setBeatSync(');
	assert.notEqual(setBeatSyncAt, -1);
	const body = rust.slice(setBeatSyncAt, rust.indexOf('\nasync function ', setBeatSyncAt + 1));
	assert.match(body, /beatSyncReenableAction\(/, 'the Rust engine takes the same policy');
	assert.match(
		body,
		/const followers = _lockedFollowers\(deck\);\s*await _reanchor\(deck, followers\)/,
		'a Rust master press re-snaps its followers'
	);
	const tickAt = rust.indexOf('export function phaseLockTick(): void {');
	assert.notEqual(tickAt, -1);
	const rustTick = rust.slice(tickAt, rust.indexOf('\n}\n', tickAt));
	assert.match(
		rustTick,
		/_checkSyncDrift\(/,
		'if the Rust 30 Hz phase-lock tick never measures drift then SYNC_DRIFT never fires on the Rust engine'
	);
});
