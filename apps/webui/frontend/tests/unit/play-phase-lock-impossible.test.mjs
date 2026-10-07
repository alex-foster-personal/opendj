/**
 * PLAY-27: a follower that cannot phase-lock still starts, unsynced (#5735).
 *
 * Tue 6 Oct 2026 21:21:58Z, silver preview, build 18, Beat Sync Max on:
 * "auto-play: deck 1 loaded 96450fe4ef37... but the handoff did not finish:
 * Beat Sync: no followers could phase-lock (deck 1: follower grid has no
 * phase-capable bar anchor with tempo ratio within [0.84, 1.16] for beat n=1)".
 * Under Max, AutoPlay KEEPS the follower's Beat Sync on when its preflight
 * fails (PLAY-23), so `play` joined the master's phase, the planner refused,
 * and the refusal aborted `play`.
 *
 * [if] the tempo ratio is outside the pitch window [then] the follower starts
 *   unsynced and one INFO line names why [⛔️ if play throws].
 * [if] the follower grid has no phase-capable bar anchor [then] same.
 * [if] the follower can phase-lock [then] it locks and no plain start happens.
 * [if] the join fails for any other reason [then] it still throws.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter, readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/** A real-shaped 4/4 PQTZ grid; `firstN` shifts the bar numbering. */
const grid = (bpm, beats = 96, firstN = 1) =>
	Array.from({ length: beats }, (_, i) => ({ n: ((i + firstN - 1) % 4) + 1, bpm, t: 0.1 + (i * 60) / bpm }));

let join;
let math;
let auto;
before(async () => {
	join = await loadTypeScriptModule('src/lib/player/grid-features.ts');
	math = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
	auto = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
});

/** The engine's join, minus transport: plan with the REAL planner, and fail
 * the way `_synchronizeFollowers` does when no follower could be planned. */
function engineJoin(masterGrid, followerGrid) {
	return async () => {
		try {
			math.computeFollowerSyncPlan({
				masterGrid,
				followerGrid,
				masterPositionAtSyncSec: 20,
				masterTempoRatio: 1,
				followerPositionSec: followerGrid[0].t,
				currentContextTimeSec: 0,
				syncAtContextTimeSec: 0.05,
				minFollowerTempoRatio: 0.84,
				maxFollowerTempoRatio: 1.16,
				mode: 'bar'
			});
		} catch (error) {
			throw new join.BeatSyncPhaseLockError(`Beat Sync: no followers could phase-lock (deck 1: ${error.message})`);
		}
	};
}

async function handoffPlay(masterGrid, followerGrid) {
	const calls = { plain: 0, lines: [] };
	const outcome = await join.joinPhaseOrStartUnsynced(
		1,
		engineJoin(masterGrid, followerGrid),
		async () => {
			calls.plain += 1;
		},
		(line) => calls.lines.push(line)
	);
	return { outcome, ...calls };
}

test('[PLAY-27] (a) a 25% tempo gap starts the follower unsynced with one INFO line, and Max is what kept sync on', async () => {
	assert.equal(
		auto.decideAutoPlayBeatSync({ source_beat_sync_enabled: true, phase_lock_ok: false, beat_sync_max: true }),
		'keep',
		'control: under Beat Sync Max a failed preflight keeps the follower synced, which is how play reached the join'
	);
	const r = await handoffPlay(grid(128), grid(160));
	assert.equal(r.outcome, 'unsynced', 'if play aborts on a 25% gap then the 21:21:58Z dead air comes back');
	assert.equal(r.plain, 1);
	assert.equal(r.lines.length, 1);
	assert.match(r.lines[0], /^\[beat-sync\] deck 1 starts unsynced: Beat Sync: no followers could phase-lock .*tempo ratio within \[0\.84, 1\.16\]/);
});

test('[PLAY-27] (b) a follower grid with no phase-capable bar anchor starts unsynced too', async () => {
	// Same tempo, but every beat is off the bar (no n=1 downbeat anywhere).
	const noDownbeats = grid(128).map((b) => ({ ...b, n: b.n === 1 ? 2 : b.n }));
	const r = await handoffPlay(grid(128), noDownbeats);
	assert.equal(r.outcome, 'unsynced');
	assert.equal(r.plain, 1);
	assert.match(r.lines[0], /starts unsynced: Beat Sync: no followers could phase-lock/);
});

test('[PLAY-27] (c) control: a compatible follower still phase-locks and never starts plain', async () => {
	const r = await handoffPlay(grid(128), grid(126));
	assert.equal(r.outcome, 'synced', 'if a lockable follower is started unsynced then Beat Sync broke');
	assert.equal(r.plain, 0);
	assert.equal(r.lines.length, 0);
});

test('[PLAY-27] any other join failure is not masked: it throws and nothing starts', async () => {
	let plain = 0;
	await assert.rejects(
		join.joinPhaseOrStartUnsynced(
			1,
			async () => {
				throw new Error('Beat Sync master deck 2 is neither audible nor scheduled to play');
			},
			async () => {
				plain += 1;
			},
			() => {}
		),
		/neither audible nor scheduled/
	);
	assert.equal(plain, 0, 'if a real engine fault starts the follower anyway then it is hidden');
});

test('[PLAY-27] SHAPE GUARD: the engine raises the typed refusal and every join in play goes through joinPhase', () => {
	const engine = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	assert.match(engine, /throw new BeatSyncPhaseLockError\(`Beat Sync: no followers could phase-lock/);
	assert.doesNotMatch(engine, /throw new Error\(`Beat Sync: no followers could phase-lock/);
	const play = engineBlockAfter('	async play(deck: DeckId, pressT0Ms?: number, startAtContextSec?: number): Promise<void> {');
	assert.doesNotMatch(play, /await _synchronizeFollowers\(/, 'if play joins without joinPhase then a refusal aborts it again');
	assert.equal(play.split('await joinPhase(').length - 1, 3, 'all three join branches route through joinPhase');
	assert.match(play, /joinPhaseOrStartUnsynced\(deck, \(\) => _synchronizeFollowers\(master, \[deck\]/);
	// A refusal play recovers from must not flash a red deck-error banner (seen live on DL).
	assert.match(play, /_synchronizeFollowers\(master, \[deck\], \{ quietPlanRefusal: true,/);
	assert.match(engine, /if \(options\.quietPlanRefusal !== true\) deckStates\[deck\]\.sync_error = message;/);
	assert.match(engine, /if \(options\.quietPlanRefusal === true && error instanceof BeatSyncPhaseLockError\) throw error;/);
	// A master handover (AutoPlay promotes the follower) keeps the new master when the OLD
	// master cannot lock to it; seen live on DL: "auto-play: deck 2 master handover refused".
	const setMaster = engineBlockAfter('	async setDeckMaster(deck: DeckId, options?: { lock?: boolean }): Promise<void> {');
	assert.match(setMaster, /_synchronizeFollowers\(deck, followers, \{ reanchorDecks: new Set\(followers\), quietPlanRefusal: true \}\)/);
	assert.match(setMaster, /if \(!\(error instanceof BeatSyncPhaseLockError\)\) \{ _assignMaster\(previousMaster\); _masterMode = previousMode; throw error; \}/, 'if a lock refusal rolls the master back then AutoPlay never hands over');
	assert.match(engine, /_synchronizeFollowers\(next, followers, \{ reanchorDecks: new Set\(followers\), quietPlanRefusal: true \}\);\n\t\t\}\)\.catch\(\(e: unknown\) => e instanceof BeatSyncPhaseLockError \? _toastUnlocked\(e\)/);
	// Ruling on r4203758016: only an ALL-failed refusal is quiet; a partial failure brands the unlocked deck.
	assert.match(engine, /if \(options\.quietPlanRefusal === true\) for \(const f of planFailed\) deckStates\[f\.deck\]\.sync_error = f\.message;/);
	// The refusal is RED on every path (pin 9bf12adccb45: the pitch bound stays hard).
	assert.match(play, /unsynced, console\.info, _toastUnlocked\)/);
	assert.match(engine, /pushToast\(f\.notice, 'error'\)/);
	const src = readFrontendSource('src/lib/player/grid-features.ts');
	assert.match(src, /if \(!\(error instanceof BeatSyncPhaseLockError\)\) throw error;/);
});

test('[PLAY-27] (d) Beat Sync Max on plus an out-of-range follower: sync stays lit (PLAY-23) and the follower plays unsynced', async () => {
	const decision = auto.decideAutoPlayBeatSync({ source_beat_sync_enabled: true, phase_lock_ok: false, beat_sync_max: true });
	assert.equal(decision, 'keep', 'Max must not be switched off by an impossible lock');
	const r = await handoffPlay(grid(128), grid(160));
	assert.equal(r.outcome, 'unsynced', 'if Max plus a 25% gap still aborts the handoff then the room goes silent');
	assert.equal(r.plain, 1);
});

test('[PLAY-27] (e) control: Beat Sync Max on plus a compatible follower still phase-locks', async () => {
	assert.equal(auto.decideAutoPlayBeatSync({ source_beat_sync_enabled: true, phase_lock_ok: true, beat_sync_max: true }), 'enable');
	const r = await handoffPlay(grid(128), grid(130));
	assert.equal(r.outcome, 'synced');
	assert.equal(r.plain, 0);
});
// Wed 7 Oct 2026 ~03:15Z, silver preview (alpha.1 code), Beat Sync Max on: the
// room went silent once while the MASTER was past the end of its beat grid. The
// planner refuses that position with a RangeError ("master position ... is
// outside the beat grid interval"), which must land in the same typed refusal.
for (const [label, followerBpm] of [['a compatible follower (126 BPM)', 126], ['a 25% gap (160 BPM)', 160]]) {
	test(`[PLAY-27] (f) a master past the end of its beat grid with ${label}: the follower starts unsynced, one INFO line, no throw`, async () => {
		const master = grid(128);
		const pastEnd = master[master.length - 1].t + 2;
		const calls = { plain: 0, lines: [] };
		const join_ = async () => {
			try {
				math.computeFollowerSyncPlan({
					masterGrid: master,
					followerGrid: grid(followerBpm),
					masterPositionAtSyncSec: pastEnd,
					masterTempoRatio: 1,
					followerPositionSec: grid(followerBpm)[0].t,
					currentContextTimeSec: 0,
					syncAtContextTimeSec: 0.05,
					minFollowerTempoRatio: 0.84,
					maxFollowerTempoRatio: 1.16,
					mode: 'bar'
				});
			} catch (error) {
				throw new join.BeatSyncPhaseLockError(`Beat Sync: no followers could phase-lock (deck 1: ${error.message})`);
			}
			assert.fail('control: the real planner must refuse a master position past its grid');
		};
		const outcome = await join.joinPhaseOrStartUnsynced(1, join_, async () => { calls.plain += 1; }, (line) => calls.lines.push(line));
		assert.equal(outcome, 'unsynced', 'if a past-grid master still aborts play then the 03:15Z silence comes back');
		assert.equal(calls.plain, 1);
		assert.equal(calls.lines.length, 1);
		assert.match(calls.lines[0], /starts unsynced: .*master position [\d.]+ is outside the beat grid interval/);
	});
}

test('[PLAY-27] (f) SHAPE GUARD: every per-follower plan failure, a past-grid RangeError included, becomes the typed refusal', () => {
	const engine = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const planAt = engine.indexOf('const plan = computeFollowerSyncPlan({');
	const catchAt = engine.indexOf('} catch (error) {', planAt);
	const pushAt = engine.indexOf('planFailed.push({ deck, message, notice:', catchAt);
	const typedAt = engine.indexOf('throw new BeatSyncPhaseLockError(`Beat Sync: no followers could phase-lock', pushAt);
	assert.ok(planAt > 0 && catchAt > planAt && pushAt > catchAt && typedAt > pushAt, 'the planner call sits in the per-follower try whose catch feeds the typed refusal');
	const handler = engine.slice(catchAt, pushAt);
	assert.doesNotMatch(handler, /throw/, 'if any planner error escapes the per-follower catch then it bypasses the unsynced start');
});

test('[PLAY-27] the red notice names the tempo gap when the pitch range cannot reach it, else the reason', () => {
	const st = (bpm, pitch = 1) => ({ bpm, pitch });
	assert.equal(join.unlockedFollowerNotice(2, st(160), st(128), 16, 'x'), 'Beat Sync cannot lock deck 2: tempo 25% away; playing unsynced');
	assert.equal(join.unlockedFollowerNotice(2, st(160), st(128, 1.25), 16, 'no phase-capable bar anchor'), 'Beat Sync cannot lock deck 2: no phase-capable bar anchor; playing unsynced', 'a reachable tempo names the reason, not a gap');
	assert.equal(join.unlockedFollowerNotice(3, st(null), st(128), 16, 'master position past the grid'), 'Beat Sync cannot lock deck 3: master position past the grid; playing unsynced');
});
