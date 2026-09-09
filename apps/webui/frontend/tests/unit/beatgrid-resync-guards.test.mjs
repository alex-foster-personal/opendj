/**
 * Behavioral tests for beatgrid-resync-guards.ts.
 *
 * The module is pure wrapping: it takes the engine's real BeatgridResyncPorts
 * plus a handful of identity probes and decides what each deferred settlement
 * is still allowed to write. Everything it wraps (reconcileAfterBeatgridSettled,
 * reconcileBeforeClear) is covered on its own in beatgrid-resync.test.mjs, so
 * these tests drive the GUARD decisions only, with fakes for the probes.
 *
 * MUTATION CHECK (each assertion below fails if the guard is removed):
 *   - beforeClear identity: reverting followerIsCurrent to a bare loadToken
 *     compare makes "a remounted session's deck is not the captured follower"
 *     fail (the fake replays token 0 in a new runtime).
 *   - adoptAuthoritativeGrid: dropping the sameBeatgrid short-circuit makes
 *     "an identical grid is not republished" fail; dropping the whole method
 *     makes the two adoption tests fail.
 *   - adoptAuthoritativeGrid source-dependent fields: reverting to spreading
 *     only `beatgrid` makes "adopts tempo_changes and performance_hints
 *     alongside beatgrid, and drops them on a switch back" fail in both
 *     directions (Codex P2 BLOCKING, PR #1587).
 *   - adoptAuthoritativeGrid skip decision: reverting the short-circuit to
 *     bare `sameBeatgrid` makes "does not skip when beats are unchanged but
 *     source/status changed" fail (Codex P2 BLOCKING, PR #1587, second
 *     round finding).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const DECKS = [1, 2, 3, 4];

let createBeatgridResyncGuards;
let sameBeatgrid;
let ScopedCommandInvalidatedError;

before(async () => {
	({ createBeatgridResyncGuards } = await loadTypeScriptModule(
		'src/lib/player/beatgrid-resync-guards.ts'
	));
	({ sameBeatgrid } = await loadTypeScriptModule('src/lib/rb/beatgrid-fallback.ts'));
	({ ScopedCommandInvalidatedError } = await loadTypeScriptModule(
		'src/lib/rb/performance-command-scheduler.ts'
	));
});

function _beats(count, bpm = 120) {
	return Array.from({ length: count }, (_, index) => ({
		n: (index % 4) + 1,
		bpm,
		t: index * (60 / bpm)
	}));
}

function _tempoChange(atS, bpmBefore, bpmAfter, confidence = 0.9) {
	return { at_s: atS, bpm_before: bpmBefore, bpm_after: bpmAfter, confidence };
}

function _anlz(stableId, beats) {
	return {
		stable_id: stableId,
		points: 0,
		waveform: {
			kind: 'mono',
			preview: { length: 0, low: [], mid: [], high: [] },
			detail: { length: 0, low: [], mid: [], high: [] }
		},
		beatgrid: { beat_count: beats.length, beats },
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' }
	};
}

/** Inert BeatgridResyncPorts. Every method is present because the guards
 * spread the real port object and beatgrid-resync.ts calls through it. */
function _ports() {
	return {
		deckIds: DECKS,
		syncMaster: () => null,
		playing: () => false,
		beatSyncEnabled: () => false,
		setBeatSyncEnabled: () => {},
		hasRealBeatGrid: () => true,
		hasSyncError: () => false,
		setSyncError: () => {},
		requiresReschedule: () => false,
		synchronizeFollowers: () => Promise.resolve(),
		hasSettledGridless: () => false,
		markSettledGridless: () => {},
		markPending: () => {},
		takePending: () => [],
		hasPendingFollowers: () => false
	};
}

/** A harness whose runtimes and load tokens the test can move independently,
 * which is the whole point: a reload bumps the token, a dispose REPLACES the
 * runtime and restarts the token from zero. */
function _harness(overrides = {}) {
	const calls = { setBeatSyncEnabled: [], setSyncError: [], markPending: [], synchronize: [] };
	const runtimes = Object.fromEntries(DECKS.map((deck) => [deck, { deck }]));
	const tokens = Object.fromEntries(DECKS.map((deck) => [deck, 0]));
	const stableIds = Object.fromEntries(DECKS.map((deck) => [deck, null]));
	const anlz = Object.fromEntries(DECKS.map((deck) => [deck, null]));
	const errors = [];
	const ports = {
		..._ports(),
		setBeatSyncEnabled: (deck, enabled) => calls.setBeatSyncEnabled.push([deck, enabled]),
		setSyncError: (deck, message) => calls.setSyncError.push([deck, message]),
		synchronizeFollowers: (master, followers) => {
			calls.synchronize.push([master, [...followers]]);
			return Promise.resolve();
		},
		markPending: (follower, master) => calls.markPending.push([follower, master]),
		...overrides
	};
	const guards = createBeatgridResyncGuards({
		ports,
		deckRuntime: (deck) => runtimes[deck],
		deckLoadToken: (deck) => tokens[deck],
		deckStableId: (deck) => stableIds[deck],
		deckAnlz: (deck) => anlz[deck],
		publishDeckAnlz: (deck, next) => (anlz[deck] = next),
		reportError: (message) => errors.push(message)
	});
	// The claim is granted a turn LATE on purpose: every finding on this surface
	// is about what changes in that gap. Scope ORDERING itself is
	// scoped-sync-runner's own contract, tested there, not re-faked here.
	guards.installScopedSyncRunner(async (deck, task) => {
		await Promise.resolve();
		await task(async (work) => await work());
	});
	return { anlz, calls, errors, guards, runtimes, stableIds, tokens };
}

test('beforeClear excludes a follower that reloaded before the deferred reconciliation ran', async () => {
	// No sync master: reconcileBeforeClear abandons every stranded follower
	// outright, so both decks WOULD be written to without the identity guard.
	// beatSyncEnabled: true - both followers currently want sync, so the
	// (separate) recheck-before-abandoning guard does not itself suppress
	// either write; this test is isolating the IDENTITY guard alone.
	const h = _harness({ syncMaster: () => null, beatSyncEnabled: () => true });
	h.guards.beforeClear(1, [2, 3], 'unload');
	h.tokens[2] += 1; // deck 2 reloaded in the gap; deck 3 did not
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(
		h.calls.setBeatSyncEnabled,
		[[3, false]],
		'only the still-current follower may be disabled - a replacement track must not inherit the abandon ' +
			'that belonged to the track it replaced (discussion_r3914921228)'
	);
	assert.deepEqual(h.calls.setSyncError.map(([deck]) => deck), [3]);
});

test('beforeClear treats a remounted session as a DIFFERENT deck even when its load token replays', async () => {
	const h = _harness({ syncMaster: () => null });
	h.guards.beforeClear(1, [2], 'unload');
	// dispose(): a brand new runtime object per deck, and the load token
	// restarts at 0 - the exact number captured a moment ago. A bare numeric
	// compare reads this as "still the same follower" (discussion_r3919692507).
	h.runtimes[2] = { deck: 2, session: 2 };
	h.tokens[2] = 0;
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(
		h.calls.setBeatSyncEnabled,
		[],
		'a replayed loadToken in a fresh runtime must not read as the captured follower - the runtime object ' +
			'is the non-reusable half of the identity pair'
	);
});

test('beforeClear abandons stranded followers when a replacement track wins the deck id under the widened reconciliation (discussion_r3920394933)', async () => {
	// A PLAY for a replacement track can elect it master under the SAME deck
	// id before this detached, widened reconciliation actually runs. Reading
	// that as "master === deck, already reconciled" would silently discard
	// the followers this deck's OLD track left stranded - they were never
	// waiting on the replacement, and nothing else will ever abandon or retry
	// them once this pre-clear path skips.
	const h = _harness({ syncMaster: () => 1, beatSyncEnabled: () => true });
	h.guards.beforeClear(1, [2, 3], 'unload');
	h.tokens[1] += 1;
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(
		h.calls.setBeatSyncEnabled,
		[
			[2, false],
			[3, false]
		],
		'a replacement track occupying the same deck id must not be read as the master these followers were ' +
			'waiting on - they must be abandoned now, since no future settlement is coming for the track that left them'
	);
});

test('beforeClear treats a published replacement track as a different master when its runtime and token are retained', async () => {
	const h = _harness({ syncMaster: () => 1, beatSyncEnabled: () => true });
	h.stableIds[1] = 'outgoing-track';
	h.guards.beforeClear(1, [2, 3], 'reload');
	// load() increments its token before `_clearLoadedTrackState()` captures
	// identities, then publishes the replacement without another token/runtime
	// change. The stable ID is the published-track half of this identity.
	h.stableIds[1] = 'replacement-track';
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(
		h.calls.setBeatSyncEnabled,
		[
			[2, false],
			[3, false]
		],
		'a replacement published under the same runtime/token must not become the master whose outgoing followers are discarded'
	);
});

test('beforeClear still no-ops when deck genuinely IS its own unchanged master at reconciliation time', async () => {
	const h = _harness({ syncMaster: () => 1, beatSyncEnabled: () => true });
	h.guards.beforeClear(1, [2, 3], 'unload');
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.deepEqual(
		h.calls.setBeatSyncEnabled,
		[],
		'an unchanged master===deck must still be left alone - a master-branch resync elsewhere recomputes this ' +
			'set from scratch, so this control must not regress into abandoning it too'
	);
});

test('beforeClear on an empty stranded list claims no scope at all', () => {
	let claimed = 0;
	const h = _harness();
	const guards = createBeatgridResyncGuards({
		ports: _ports(),
		deckRuntime: (deck) => h.runtimes[deck],
		deckLoadToken: (deck) => h.tokens[deck],
		deckStableId: () => null,
		deckAnlz: () => null,
		publishDeckAnlz: () => {},
		reportError: () => {}
	});
	guards.installScopedSyncRunner(() => {
		claimed += 1;
		return Promise.resolve();
	});
	guards.beforeClear(1, [], 'unload');
	assert.equal(claimed, 0, 'claiming the all-scope barrier for an empty reconciliation wastes it');
});

test('adoptAuthoritativeGrid replaces a deck fallback grid with the authoritative one', async () => {
	const h = _harness();
	h.stableIds[1] = 'sid-a';
	h.anlz[1] = _anlz('sid-a', _beats(8, 128));
	const authoritative = _anlz('sid-a', _beats(16, 124));
	h.guards.adoptAuthoritativeGrid('sid-a', authoritative);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(h.anlz[1].beatgrid.beats.length, 16, 'deck.anlz must adopt the authoritative grid');
	assert.equal(
		h.anlz[1].beatgrid.beats[0].bpm,
		124,
		'the beat math reads deck.anlz - adopting only in the WaveRow paint leaves quantize and Beat Sync on ' +
			'the stale fallback (discussion_r3919779323)'
	);
});

test('adoptAuthoritativeGrid carries tempo_changes and performance_hints alongside beatgrid, both ways', async () => {
	// Own-to-own: an own dynamic grid must land WITH its markers, not a bare
	// beatgrid stranding the deck's stale (or absent) tempo_changes.
	const h = _harness();
	h.stableIds[1] = 'sid-a';
	h.anlz[1] = { ..._anlz('sid-a', _beats(8, 128)), tempo_changes: [_tempoChange(0, 120, 128)] };
	const ownGrid = {
		..._anlz('sid-a', _beats(16, 124)),
		tempo_changes: [_tempoChange(0, 120, 124), _tempoChange(10, 124, 126)],
		performance_hints: { dynamic_tempo: true }
	};
	h.guards.adoptAuthoritativeGrid('sid-a', ownGrid);
	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(
		h.anlz[1].tempo_changes,
		ownGrid.tempo_changes,
		'an own dynamic grid must not be installed without the tempo markers it came with'
	);
	assert.deepEqual(h.anlz[1].performance_hints, { dynamic_tempo: true });

	// Own-to-rekordbox: switching sources back must not retain the stale
	// own-only markers a rekordbox payload never carries.
	const rekordboxGrid = _anlz('sid-a', _beats(8, 128));
	h.guards.adoptAuthoritativeGrid('sid-a', rekordboxGrid);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(
		h.anlz[1].tempo_changes,
		undefined,
		'a deck switched back to rekordbox must not retain stale own-only dynamic tempo metadata'
	);
	assert.equal(h.anlz[1].performance_hints, undefined);
});

test('adoptAuthoritativeGrid does not skip when beats are unchanged but source/status changed', async () => {
	// An empty rekordbox grid and an own `missing` result are both `beats:
	// []`, so a beats-only equality check reads them as identical - exactly
	// the case Codex's follow-up finding names (PR #1587).
	const h = _harness();
	h.stableIds[1] = 'sid-a';
	h.anlz[1] = { ..._anlz('sid-a', []), beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] } };
	const ownMissing = {
		..._anlz('sid-a', []),
		beatgrid: { source: 'own', beat_count: 0, beats: [], status: 'missing', reason: null }
	};
	h.guards.adoptAuthoritativeGrid('sid-a', ownMissing);
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(
		h.anlz[1].beatgrid.source,
		'own',
		'identical (empty) beats arrays must not mask a real source/status transition'
	);
	assert.equal(h.anlz[1].beatgrid.status, 'missing');
});

test('adoptAuthoritativeGrid does not republish a grid the deck already holds', async () => {
	const h = _harness();
	h.stableIds[1] = 'sid-a';
	const beats = _beats(16, 124);
	h.anlz[1] = _anlz('sid-a', beats);
	const before = h.anlz[1];
	h.guards.adoptAuthoritativeGrid('sid-a', _anlz('sid-a', _beats(16, 124)));
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(
		h.anlz[1],
		before,
		'the ambient /anlz retry republishes the same grid every cooldown - re-running reconciliation for it ' +
			'would churn an already locked follower for no reason'
	);
});

test('adoptAuthoritativeGrid ignores decks holding a different track', async () => {
	const h = _harness();
	h.stableIds[1] = 'sid-a';
	h.stableIds[2] = 'sid-b';
	h.anlz[1] = _anlz('sid-a', _beats(8, 128));
	h.anlz[2] = _anlz('sid-b', _beats(8, 128));
	h.guards.adoptAuthoritativeGrid('sid-a', _anlz('sid-a', _beats(16, 124)));
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(h.anlz[2].beatgrid.beats.length, 8, 'only the deck holding that stable_id may be touched');
});

test('adoptAuthoritativeGrid skips a deck that reloaded before the claim was granted', async () => {
	const h = _harness();
	h.stableIds[1] = 'sid-a';
	h.anlz[1] = _anlz('sid-a', _beats(8, 128));
	const guards = createBeatgridResyncGuards({
		ports: _ports(),
		deckRuntime: (deck) => h.runtimes[deck],
		deckLoadToken: (deck) => h.tokens[deck],
		deckStableId: (deck) => h.stableIds[deck],
		deckAnlz: (deck) => h.anlz[deck],
		publishDeckAnlz: (deck, next) => (h.anlz[deck] = next),
		reportError: () => {}
	});
	// Reload the deck inside the gap between submission and the claim.
	guards.installScopedSyncRunner(async (deck, task) => {
		h.tokens[deck] += 1;
		await task((work) => work());
	});
	guards.adoptAuthoritativeGrid('sid-a', _anlz('sid-a', _beats(16, 124)));
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(
		h.anlz[1].beatgrid.beats.length,
		8,
		'a replacement track must never inherit the grid adopted for the track it replaced'
	);
});

test('adoptAuthoritativeGrid skips a published replacement that retained its new load token', async () => {
	const h = _harness();
	h.stableIds[1] = 'sid-a';
	h.anlz[1] = _anlz('sid-a', _beats(8, 128));
	// load(B) increments the token before it clears A. The ambient retry for A
	// therefore captures B's token while A is still published. B can then be
	// published without another token change before the scoped claim runs.
	h.tokens[1] += 1;
	h.guards.adoptAuthoritativeGrid('sid-a', _anlz('sid-a', _beats(16, 124)));
	h.stableIds[1] = 'sid-b';
	h.anlz[1] = _anlz('sid-b', _beats(8, 132));
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(
		h.anlz[1].beatgrid.beats.length,
		8,
		'a published replacement must not inherit the authoritative grid for the track it replaced'
	);
});

test('sameBeatgrid is exact, not a beat-count heuristic', () => {
	const left = { beat_count: 4, beats: _beats(4, 120) };
	assert.equal(sameBeatgrid(left, { beat_count: 4, beats: _beats(4, 120) }), true);
	assert.equal(
		sameBeatgrid(left, { beat_count: 4, beats: _beats(4, 121) }),
		false,
		'same beat COUNT with different tempo is a different measurement'
	);
	const shifted = _beats(4, 120).map((beat) => ({ ...beat, t: beat.t + 0.01 }));
	assert.equal(
		sameBeatgrid(left, { beat_count: 4, beats: shifted }),
		false,
		'a phase-shifted grid of the same length is a different measurement'
	);
	assert.equal(sameBeatgrid(left, { beat_count: 5, beats: _beats(5, 120) }), false);
});

test('a route unmount invalidating the adoption claim is consumed, not toasted', async () => {
	// discussion_r3920002298 (P2): every claim still queued when the route
	// unmounts rejects with ScopedCommandInvalidatedError. That is the
	// scheduler working. Left unhandled on this fire-and-forget call it is an
	// unhandled promise rejection on ordinary navigation; reported, it is a
	// toast for something the DJ did on purpose.
	const h = _harness();
	h.stableIds[1] = 'sid-a';
	h.anlz[1] = _anlz('sid-a', _beats(8, 128));
	const rejections = [];
	const onRejection = (error) => rejections.push(error);
	process.on('unhandledRejection', onRejection);
	h.guards.installScopedSyncRunner(() =>
		Promise.reject(new ScopedCommandInvalidatedError('performance route unmounted'))
	);
	h.guards.adoptAuthoritativeGrid('sid-a', _anlz('sid-a', _beats(16, 124)));
	await new Promise((resolve) => setTimeout(resolve, 20));
	process.off('unhandledRejection', onRejection);
	assert.deepEqual(rejections, [], 'an invalidated claim must not escape as an unhandled rejection');
	assert.deepEqual(h.errors, [], 'a deliberate route unmount is not a failure to report');
});

test('a real adoption failure is still reported loudly', async () => {
	const h = _harness();
	h.stableIds[1] = 'sid-a';
	h.anlz[1] = _anlz('sid-a', _beats(8, 128));
	h.guards.installScopedSyncRunner(() => Promise.reject(new Error('scheduler exploded')));
	h.guards.adoptAuthoritativeGrid('sid-a', _anlz('sid-a', _beats(16, 124)));
	await new Promise((resolve) => setTimeout(resolve, 20));
	assert.equal(h.errors.length, 1, 'anything that is not an invalidation stays loud');
	assert.match(h.errors[0], /scheduler exploded/);
});
