import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc;
let pairing;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	pairing = await loadTypeScriptModule('tests/unit/fixtures/pairing-snapshot-entry.ts', {
		viteApiBase: 'https://pairing.example.test'
	});
});

test('queue scopes isolate deck loads and coordinate only sync-sensitive commands', () => {
	assert.deepEqual(ipc.PERFORMANCE_PRESET_COMMAND_SCOPES, [
		1, 2, 3, 4, 'persistence-1', 'persistence-2', 'persistence-3', 'persistence-4', 'sync', 'headphone'
	]);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'load', deck: 1, stable_id: 'a' }), [
		1
	]);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'pitch_range', deck: 1, range: 8 }),
		[1]
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'quantize', deck: 2, enabled: false }),
		[2]
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'beat_loop', deck: 2, beats: 4 }),
		[2]
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({
			type: 'stem_mute',
			deck: 2,
			stem: 'instrumental',
			muted: true
		}),
		null
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'seek', deck: 3, position_ms: 1000 }),
		[3, 'sync']
	);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'master', deck: 4 }), [4, 'sync']);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'hot_cue_save', deck: 4, slot: 'A', in_ms: 1000, revision: 'etag' }),
		['persistence-4']
	);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'key_sync', deck: 4 }), [4, 'sync']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'channel_cue', deck: 4, enabled: true }), [4]);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'headphone_outputs_refresh' }), ['headphone']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'headphone_output_acquire' }), ['headphone']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'headphone_output_select', device_id: 'usb' }), ['headphone']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'headphone_master_select', device_id: 'speakers' }), ['headphone']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'headphone_input_select', device_id: 'mic' }), ['headphone']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'output_mode', mode: 'practice' }), ['headphone']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'output_mode', mode: 'two_outputs' }), ['headphone']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'output_mode', mode: 'split_cable' }), ['headphone']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'head_delay_ms', value: 40 }), ['headphone']);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'analysis_source', feature: 'beatgrid', source: 'own' }),
		[1, 2, 3, 4, 'sync']
	);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'key_nudge', deck: 4, semitones: -1 }), [4, 'sync']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'slip', deck: 4, enabled: true }), [4]);
	assert.equal(ipc.performanceCommandQueueScopes({ type: 'trim', deck: 2, value: 0.7 }), null);
	assert.equal(ipc.performanceCommandQueueScopes({ type: 'filter', deck: 2, value: 0.7 }), null);
	assert.equal(ipc.performanceCommandQueueScopes({ type: 'crossfader', value: 0.3 }), null);
	assert.equal(ipc.performanceCommandQueueScopes({ type: 'pairing_snapshot_open' }), null);
	assert.equal(
		ipc.performanceCommandQueueScopes({ type: 'pairing_snapshot_remove_eq_adjuster', deck: 1, band: 'low' }),
		null
	);
});

test('load suppressCommandErrorToast parses through parsePerformanceCommandForTest', () => {
	const cmd = ipc.parsePerformanceCommandForTest({
		type: 'load',
		deck: 1,
		stable_id: 'track-a',
		suppressCommandErrorToast: true
	});
	assert.deepEqual(cmd, {
		type: 'load',
		deck: 1,
		stable_id: 'track-a',
		suppressCommandErrorToast: true
	});
	assert.throws(
		() =>
			ipc.parsePerformanceCommandForTest({
				type: 'load',
				deck: 1,
				stable_id: 'track-a',
				extra: true
			}),
		/unexpected fields/
	);
});

test('pairing snapshot freezes deck state, removes only real adjustments, and saves only the selected pair', async () => {
	const originalFetch = globalThis.fetch;
	const requests = [];
	globalThis.fetch = async (request) => {
		const body = JSON.parse(await request.clone().text());
		requests.push(body);
		return new Response(JSON.stringify({
			pairing_id: 'pairing-1', ...body, direction: '->', source: 'manual', notes: null,
			created_at: '2026-09-05T00:00:00.000000Z', updated_at: '2026-09-05T00:00:00.000000Z'
		}), { status: 201, headers: { 'content-type': 'application/json' } });
	};
	globalThis.window = {};
	const uninstall = pairing.installPerformanceBrowserIpc();
	try {
		pairing.uiPrefs.beat_sync_max = false;
		for (const deckId of [1, 2, 3, 4]) {
			const deck = pairing.deckStates[deckId];
			deck.stable_id = deckId <= 3 ? `track-${deckId}` : null;
			deck.title = deckId <= 3 ? `Track ${deckId}` : null;
			deck.position_ms = deckId * 1000;
			deck.anlz = null;
			Object.assign(pairing.mixerState.channels[deckId], { eq_low: 0.2, eq_mid: 0.5, eq_high: 0.8 });
		}
		await pairing.dispatchPerformanceCommand({ type: 'pairing_snapshot_open' });
		pairing.deckStates[1].position_ms = 99999;
		pairing.mixerState.channels[1].eq_low = 0.9;
		await pairing.dispatchPerformanceCommand({ type: 'pairing_snapshot_remove_eq_adjuster', deck: 1, band: 'low' });
		await assert.rejects(
			pairing.dispatchPerformanceCommand({ type: 'pairing_snapshot_remove_eq_adjuster', deck: 1, band: 'mid' }),
			/has no mid EQ adjustment/
		);
		await pairing.dispatchPerformanceCommand({ type: 'pairing_snapshot_save', from_deck: 1, to_deck: 3 });
		const snapshot = pairing.queryPerformanceState().pairing_snapshot;
		assert.equal(snapshot.decks[0].position_ms, 1000);
		assert.deepEqual(snapshot.decks[0].eq_adjusts, [{ band: 'high', value: 0.8 }]);
		assert.deepEqual(requests[0].snapshot.decks.map((deck) => deck.deck_id), [1, 3]);
	} finally {
		uninstall();
		globalThis.fetch = originalFetch;
		delete globalThis.window;
	}
});

test('pairing snapshot falls back to time units when a deck has no beat to stamp', async () => {
	globalThis.window = {};
	const uninstall = pairing.installPerformanceBrowserIpc();
	try {
		pairing.uiPrefs.beat_sync_max = true;
		for (const deckId of [1, 2, 3, 4]) {
			pairing.deckStates[deckId].stable_id = null;
			pairing.deckStates[deckId].anlz = null;
		}
		pairing.deckStates[1].stable_id = 'track-no-grid';
		pairing.deckStates[1].title = 'No Grid';
		pairing.deckStates[1].position_ms = 500;
		pairing.deckStates[1].anlz = { beatgrid: { beats: [] } };
		// Deck 2 HAS a grid but sits before its first beat, as a fresh load does.
		pairing.deckStates[2].stable_id = 'track-at-zero';
		pairing.deckStates[2].title = 'At Zero';
		pairing.deckStates[2].position_ms = 0;
		pairing.deckStates[2].anlz = { beatgrid: { beats: [{ n: 1, bpm: 128, t: 0.135 }] } };
		await pairing.dispatchPerformanceCommand({ type: 'pairing_snapshot_open' });
		const snapshot = pairing.queryPerformanceState().pairing_snapshot;
		assert.equal(snapshot.beat_sync_max, false);
		assert.deepEqual(
			snapshot.decks.map((deck) => [deck.deck_id, deck.timestamp.unit, deck.timestamp.value]),
			[[1, 'time', 500], [2, 'time', 0]]
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('a beatgrid-landed resync claims only its own deck, and widening never makes that claim wait on the wide work', () => {
	// PARITY-09 / PR #765 P1 ("Route deferred resync through the scoped
	// scheduler") + three further BLOCKING findings on the fix itself ("Claim
	// follower scopes during the deferred resync", then r3913350814, then
	// r3913492572, then r3913693383): audio-engine.svelte.ts's deferred
	// beatgrid upgrade fires its resync from a continuation that runs well
	// after the load() command already released its own [deck] scope. That
	// callback cannot import _commandScheduler directly (this module already
	// imports audio-engine.svelte.ts, so the reverse import would be cycle #2
	// against frontend.import_cycles' floor of 1) - so the engine calls back
	// into a runner installed here, and that runner must be the SAME
	// scheduler every other performance command uses, not a bypass.
	//
	// r3913693383 P1 BLOCKING "keep narrow settlements off unrelated deck
	// tails": an earlier design reserved [_deck] plus every other deck and
	// 'sync' up front on EVERY settlement, even the common narrow one that
	// never touches them - rejected as an unacceptable latency/independence
	// cost. This claim must reserve [_deck] ALONE.
	//
	// r3913492572 P1 BLOCKING "no acquisition that can observe a successor
	// registered in between": claiming [_deck] alone, then LATER claiming the
	// wide scopes from a nested call AWAITED by this claim, can deadlock - a
	// successor submitted in between that shares [_deck] depends on this
	// claim, and this claim depending on the wide work too (once the
	// successor has taken a wide scope first) closes a cycle. The fix is
	// that `widen`'s wide claim must never be awaited or returned by this
	// claim's own body - fired and left detached, this claim's own tail
	// resolves independent of the wide work, breaking the only edge that
	// could close the cycle.
	const source = readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	const start = source.indexOf('installScopedSyncRunner((_deck, run) => {');
	assert.ok(start > 0, 'installScopedSyncRunner call not found - this test is reading the wrong file');
	const end = source.indexOf('\nlet _commandGeneration', start);
	assert.ok(end > start, 'end of installScopedSyncRunner call not found');
	const body = source.slice(start, end);
	assert.match(
		body,
		/_commandScheduler\s*\n?\s*\.run\(\[_deck\],/,
		'installScopedSyncRunner must claim ONLY [_deck] up front - reserving the wide scopes too, ' +
			'even for a settlement that turns out narrow, reopens the r3913693383 finding'
	);
	assert.doesNotMatch(body, /\breserve\(/, 'installScopedSyncRunner must not call the removed reserve() primitive');
	const widenStart = body.indexOf('const widen: WidenScope = (work) => {');
	assert.ok(widenStart > 0, 'widen closure not found');
	const runCallStart = body.indexOf('.run([_deck],');
	assert.ok(runCallStart > widenStart, 'widen must be defined BEFORE the [_deck] claim so it can be passed in, never awaited by it');
	const widenBody = body.slice(widenStart, runCallStart);
	assert.match(
		widenBody,
		/_commandScheduler\.run\(\[\.\.\.DECK_IDS, 'sync'\],/,
		"widen must fire a SEPARATE top-level claim for every deck (INCLUDING [_deck] itself, r3914267990) plus 'sync' - " +
			'excluding [_deck] leaves a gap, the instant this claim releases it, where a fresh command on the same deck ' +
			'can race the still in-flight wide reconciliation'
	);
	assert.match(
		widenBody,
		/await work\(\)/,
		'the wide claim must still actually run the caller-supplied work, even once wrapped in its own ' +
			'queued/active bookkeeping (discussion_r3914921214)'
	);
	const outerClaimBody = body.slice(runCallStart);
	assert.doesNotMatch(
		outerClaimBody,
		/await widen\(/,
		"the [_deck] claim's own body must never await widen(...) - doing so reopens the r3913492572 deadlock"
	);
});

test('the deferred resync is counted in performanceCommandStatus while it holds the scheduler, not invisible to it', () => {
	// P1 raised on re-review, Tue 2 Sep 2026: every OTHER caller of
	// _commandScheduler.run (_enqueuePresetPhase, the regular command
	// dispatcher) bumps queued/active/deck_pending around the call so
	// startPerformancePresetTransaction's `state.command_pending ||
	// state.command_queued !== 0` readiness check can see in-flight work. The
	// installScopedSyncRunner callback claims the SAME scheduler scope but,
	// before this fix, was a bare pass-through with none of that bookkeeping -
	// so a preset transaction could observe command_pending === false and
	// publish 'ready' while this resync was still queued or running behind
	// it, letting it silently alter transport schedules after the preset
	// claimed to be settled. Bookkeeping now bumps [_deck] around the outer
	// claim and, separately, every deck (including [_deck] again, r3914267990:
	// the widen closure's own claim now covers [_deck] too, so its pending
	// count must stay elevated across the same span) around the widen closure -
	// never an unconditional DECK_IDS sweep for the OUTER claim, which would
	// count a narrow-scope resync against decks it never actually claimed.
	//
	// discussion_r3914921214 (P1 BLOCKING, further re-review): the widen
	// closure bumped deck_pending for the wide phase but never queued/active,
	// so queryPerformanceState()'s AGGREGATE command_pending (active > 0 ||
	// queued > 0) stayed false the entire time the wide reconciliation was
	// queued or running - only the PER-DECK command_pending (deck_pending >
	// 0) saw it. A preset transaction's readiness check reads the aggregate
	// field, so it could still publish 'ready' while the wide phase ran. The
	// wide phase now gets its own queued-to-active transition, mirroring the
	// outer [_deck] claim's, so the aggregate field sees it too.
	const source = readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	const start = source.indexOf('installScopedSyncRunner((_deck, run) => {');
	assert.ok(start > 0, 'installScopedSyncRunner call not found - this test is reading the wrong file');
	const end = source.indexOf('\nlet _commandGeneration', start);
	const body = source.slice(start, end);
	for (const bump of [
		'performanceCommandStatus.queued += 1',
		'performanceCommandStatus.deck_pending[_deck] += 1',
		'performanceCommandStatus.active += 1',
		'performanceCommandStatus.active -= 1',
		'performanceCommandStatus.deck_pending[_deck] -= 1',
		'for (const deck of DECK_IDS) performanceCommandStatus.deck_pending[deck] += 1',
		'for (const deck of DECK_IDS) performanceCommandStatus.deck_pending[deck] -= 1'
	]) {
		assert.ok(
			body.includes(bump),
			`installScopedSyncRunner's callback must include "${bump}" - otherwise the ` +
				'deferred resync is invisible to command_pending/command_queued while it ' +
				'holds the scheduler scope, or counts against decks it never actually claimed'
		);
	}
	const widenStart = body.indexOf('const widen: WidenScope = (work) => {');
	assert.ok(widenStart > 0, 'widen closure not found');
	const widenBody = body.slice(widenStart, body.indexOf('.run([_deck],'));
	assert.ok(
		widenBody.includes('performanceCommandStatus.queued += 1'),
		"widen's own wide claim must bump queued when it is scheduled, not just deck_pending - " +
			'otherwise queryPerformanceState()\'s aggregate command_pending (active > 0 || queued > 0) ' +
			'never sees the wide phase, even though the per-deck field does (discussion_r3914921214)'
	);
	assert.ok(
		widenBody.includes('performanceCommandStatus.active += 1') &&
			widenBody.includes('performanceCommandStatus.active -= 1'),
		"widen's own wide claim must transition queued -> active once its work actually starts, " +
			'mirroring the outer [_deck] claim (discussion_r3914921214)'
	);
});

test('startPerformancePresetTransaction and stopPerformancePresetTransaction poll for the queue to drain before their readiness check', () => {
	// discussion_r3919212909 (P1 BLOCKING): a settlement's narrow [_deck] claim
	// can detach (its own command() resolves) before widen() has even been
	// called, so the wide claim only registers itself in the scheduler's tail
	// map at that later point. If a preset START/STOP claim is submitted in
	// between, it already took over the tail map for every scope the wide
	// claim needs, so the wide claim ends up waiting on the PRESET's tail
	// while the preset itself only ever waited on the narrow claim. The
	// preset's own claim releases its tail (letting the wide claim start)
	// BEFORE _enqueuePresetPhase's await in startPerformancePresetTransaction/
	// stopPerformancePresetTransaction returns, so a single synchronous
	// idleness check right after it reads the wide claim as still
	// queued/active EVERY time this ordering occurs - not a rare race, a
	// guaranteed one, since the very fix that made the wide claim visible to
	// command_pending/command_queued (r3914921214, the test above) is what
	// turned this into a hard, deterministic throw instead of a silent miss.
	const source = readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	const pollFnStart = source.indexOf('async function _pollUntilSettled(');
	assert.ok(pollFnStart > 0, '_pollUntilSettled not found - this test is reading the wrong file');
	const pollFnEnd = source.indexOf('\nasync function _awaitCommandQueueIdle', pollFnStart);
	assert.ok(pollFnEnd > pollFnStart, '_awaitCommandQueueIdle marker not found after _pollUntilSettled');
	const pollFnBody = source.slice(pollFnStart, pollFnEnd);
	assert.ok(
		pollFnBody.includes('setTimeout') &&
			pollFnBody.includes('requestAnimationFrame') &&
			pollFnBody.includes('Promise.race'),
		'the shared preset settle poll must race a timer-backed deadline against each requestAnimationFrame wait ' +
			'(discussion_r3919293437 P2 BLOCKING) - a bound checked only before awaiting rAF never actually ' +
			'expires on a hidden/backgrounded tab, since Chromium can suspend rAF callbacks indefinitely while ' +
			'setTimeout keeps firing, so a plain rAF-only poll can leave a preset transaction, including ' +
			'unmount stop cleanup, pending forever even after the command queue has drained'
	);
	const idleFnStart = source.indexOf('async function _awaitCommandQueueIdle(');
	assert.ok(idleFnStart > 0, '_awaitCommandQueueIdle not found - this test is reading the wrong file');
	const idleFnBody = source.slice(idleFnStart, source.indexOf('\n/**', idleFnStart));
	assert.ok(
		idleFnBody.includes('!state.command_pending && state.command_queued === 0'),
		'_awaitCommandQueueIdle must poll queryPerformanceState() itself, not a snapshot taken once before the loop'
	);
	for (const [name, marker] of [
		['startPerformancePresetTransaction', 'export async function startPerformancePresetTransaction<T>('],
		['stopPerformancePresetTransaction', 'export async function stopPerformancePresetTransaction<T>(']
	]) {
		const fnStart = source.indexOf(marker);
		assert.ok(fnStart > 0, `${name} not found - this test is reading the wrong file`);
		const fnEnd = source.indexOf('\nexport', fnStart + 1);
		const fnBody = source.slice(fnStart, fnEnd);
		const enqueueIndex = fnBody.indexOf('await _enqueuePresetPhase(id, work)');
		const idleIndex = fnBody.indexOf('await _awaitCommandQueueIdle()');
		const checkIndex = fnBody.indexOf('_assertPresetSettled(id,');
		assert.ok(enqueueIndex > 0, `${name} must call _enqueuePresetPhase`);
		assert.ok(
			idleIndex > enqueueIndex && idleIndex < checkIndex,
			`${name} must await _awaitCommandQueueIdle() AFTER _enqueuePresetPhase resolves and BEFORE the ` +
				'readiness check, so a wide claim that only just became visible to command_pending/' +
				'command_queued gets a real chance to drain instead of failing the check on its first, ' +
				'guaranteed-non-idle read'
		);
	}
});

test('a preset publishes ready only once every deck has presented its desired schedule revision', () => {
	// discussion_r3919293425 (P1 BLOCKING): draining the command queue is not the
	// same guarantee as reaching the audio output. A settlement whose narrow
	// [_deck] claim preceded START, but which only registered its WIDE claim
	// after START had claimed every scope, sits BEHIND the preset in the tail
	// map - so it runs after startPerformancePreset already finished its own
	// waitForPerformancePresetPresented, and its cross-deck phase-lock can
	// acknowledge a fresh schedule revision on any deck it touches. Waiting for
	// the queue makes that reconciliation FINISH; it does not make its revision
	// audible. AGENTS.md's preset contract is explicit that ready comes only
	// "after all decks are audible with matching desired/presented revisions and
	// an idle command queue", and the revision half was never rechecked here.
	//
	// Restoring the settlement's queue POSITION instead would mean claiming the
	// wide scopes up front, at submission time - the design r3913693383 already
	// rejected as an unacceptable latency and independence cost, and the exact
	// opposite of what r3919411682 asks for. So the fix is the postcondition.
	const source = readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	const assertStart = source.indexOf('function _assertPresetSettled(');
	assert.ok(assertStart > 0, '_assertPresetSettled not found - this test is reading the wrong file');
	const assertBody = source.slice(assertStart, source.indexOf('\nfunction _recordPresetFailure', assertStart));
	assert.ok(
		assertBody.includes('state.command_pending || state.command_queued !== 0'),
		'the readiness assertion must still reject a non-idle command queue'
	);
	assert.ok(
		assertBody.includes('performanceDecksAwaitingPresentation(state)'),
		'the readiness assertion must ALSO reject a deck whose desired schedule revision has not been presented'
	);
	for (const [name, marker] of [
		['startPerformancePresetTransaction', 'export async function startPerformancePresetTransaction<T>('],
		['stopPerformancePresetTransaction', 'export async function stopPerformancePresetTransaction<T>(']
	]) {
		const fnStart = source.indexOf(marker);
		const fnBody = source.slice(fnStart, source.indexOf('\nexport', fnStart + 1));
		const idleIndex = fnBody.indexOf('await _awaitCommandQueueIdle()');
		const presentedIndex = fnBody.indexOf('await _awaitPresentedScheduleRevisions()');
		const assertIndex = fnBody.indexOf('_assertPresetSettled(id,');
		const releaseIndex = fnBody.indexOf('_releasePreset(id,');
		assert.ok(
			presentedIndex > idleIndex && assertIndex > presentedIndex && releaseIndex > assertIndex,
			`${name} must drain the queue, then wait for every deck's desired revision to be presented, ` +
				'then assert both, and only then publish the lifecycle phase - asserting without waiting ' +
				'would throw on a reconciliation that is merely still in flight'
		);
	}
});

test('a preset settle failure raised after the work returned still mutes and stops the decks', () => {
	// discussion_r3919692501 (P1 BLOCKING): the two settle waits above run
	// AFTER _enqueuePresetPhase resolves, and they have to - holding the
	// all-scope claim while waiting for the widened reconciliation that needs
	// those very scopes is the r3913492572 deadlock. But that also puts them
	// outside startPerformancePreset's own rollback boundary, which closed when
	// it returned with playback already unmuted. A timeout therefore threw with
	// four decks audible, and the route's _start catch only records routeError.
	// So the transaction re-creates the boundary itself.
	const source = readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	const compStart = source.indexOf('async function _compensateLateSettlement(');
	assert.ok(compStart > 0, '_compensateLateSettlement not found - this test is reading the wrong file');
	const compBody = source.slice(compStart, source.indexOf('\nfunction _recordPresetFailure', compStart));
	const muteIndex = compBody.indexOf('engine.setMaster(MUTED_MASTER_VOLUME)');
	const stopIndex = compBody.indexOf('await _enqueuePresetPhase(id, compensate)');
	assert.ok(muteIndex > 0, 'the compensation must hard-mute the master output');
	assert.ok(
		stopIndex > muteIndex,
		'the hard mute must be taken BEFORE the reverse-order stop is enqueued - the stop queues behind the ' +
			'very work that just failed to settle, so muting after it would keep the decks audible for exactly ' +
			'as long as the failure lasts (same ordering createFailClosedPerformanceTeardown uses)'
	);
	assert.ok(
		compBody.includes('_recordPresetFailure(id, failure)') && compBody.includes('throw failure'),
		'the settle failure must still be recorded and propagated - compensating must not swallow it'
	);
	assert.ok(
		compBody.includes('new AggregateError('),
		'a rollback that itself fails must be reported alongside the settle failure, not instead of it'
	);
	for (const [name, marker, phase] of [
		['startPerformancePresetTransaction', 'export async function startPerformancePresetTransaction<T>(', 'ready'],
		['stopPerformancePresetTransaction', 'export async function stopPerformancePresetTransaction<T>(', 'idle']
	]) {
		const fnStart = source.indexOf(marker);
		const fnBody = source.slice(fnStart, source.indexOf('\nexport', fnStart + 1));
		assert.ok(
			fnBody.includes('compensate: (driver: PerformancePresetTransactionDriver) => Promise<unknown>'),
			`${name} must take the caller's compensating action - only the caller knows the preset's deck order`
		);
		const catchIndex = fnBody.indexOf('await _compensateLateSettlement(id, settleError, compensate)');
		const releaseIndex = fnBody.indexOf(`_releasePreset(id, '${phase}', null)`);
		assert.ok(
			catchIndex > 0,
			`${name} must route a settle failure through _compensateLateSettlement, not let it escape to the ` +
				'route with the decks left as they were'
		);
		assert.ok(
			releaseIndex > catchIndex,
			`${name} must only publish '${phase}' after the settle block has passed`
		);
		assert.ok(
			fnBody.indexOf('await _awaitCommandQueueIdle()') > fnBody.indexOf('_enqueuePresetPhase(id, work)'),
			`${name} must not move the settle waits back inside the all-scope claim (r3913492572 deadlock)`
		);
	}
});

test('performanceDecksAwaitingPresentation names exactly the decks whose desired revision has not reached the output', () => {
	// The pure half of the fix above, so the postcondition itself is proven
	// rather than only its call order. A revision pair is the same signal
	// assertPerformancePresetPresented reads, generalized to every deck: a
	// deferred reconciliation reschedules whichever followers the sync master
	// has, and those need not be the preset's own decks.
	const fresh = ipc.queryPerformanceState();
	assert.deepEqual(
		ipc.performanceDecksAwaitingPresentation(fresh),
		[],
		'a freshly booted engine has every deck at desired 0 / presented 0 and owes the output nothing'
	);
	const clocks = { 1: [4, 4], 2: [7, 5], 3: [0, 0], 4: [2, 1] };
	const state = {
		...fresh,
		decks: Object.fromEntries(
			Object.entries(clocks).map(([deck, [desired, presented]]) => [
				deck,
				{
					...fresh.decks[deck],
					transport_clock: {
						...fresh.decks[deck].transport_clock,
						desired_revision: desired,
						presented_revision: presented
					}
				}
			])
		)
	};
	assert.deepEqual(
		ipc.performanceDecksAwaitingPresentation(state),
		[2, 4],
		'only the decks whose acknowledged schedule revision has not crossed the presentation clock may be reported'
	);
});

test('analysis-source commands are validated through the typed all-deck scheduler', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'analysis_source', feature: 'vocals', source: 'own' }),
			/feature must be beatgrid/i
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'analysis_source', feature: 'beatgrid', source: 'invalid' }),
			/source must be rekordbox or own/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('key controls validate through IPC and round-trip serializable shift state', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const initial = ipc.queryPerformanceState().decks[1];
		assert.equal(initial.key, null);
		assert.equal(initial.key_shift_semitones, 0);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'key_nudge', deck: 1, semitones: 2 }),
			/semitones must be -1 or 1/i
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'key_sync', deck: 1, enabled: true, extra: true }),
			/unexpected fields/i
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'key_sync', deck: 1 }),
			/missing keys|enabled/i
		);
		assert.equal(initial.key_sync_enabled, false);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

// requirement: PREF-02
test('vibe meter uses thumb glyphs and routes every mark through the typed dispatcher', async () => {
	const source = await readFile(new URL('../../src/lib/components/rb/VibeMeter.svelte', import.meta.url), 'utf8');
	assert.match(source, /recordFeedback\('bad', event\)/);
	assert.match(source, /event\.shiftKey \? 'great' : 'good'/);
	assert.match(source, /type: 'feedback_mark'/);
	assert.doesNotMatch(source, /voteVibe\(/);
	assert.match(source, /M5 2\.2h5\.1/);
});

test('SLIP validates through IPC and exposes its inactive read state', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const initial = ipc.queryPerformanceState().decks[1];
		assert.equal(initial.slip_enabled, false);
		assert.equal(initial.slip_active, false);
		assert.equal(initial.slip_position_ms, null);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'slip', deck: 1, enabled: 'yes' }),
			/enabled must be boolean/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('hot-cue controls are strict IPC commands with serializable slot state', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const initial = ipc.queryPerformanceState().decks[1];
		assert.equal(initial.hot_cue_slots.length, 8);
		assert.deepEqual(initial.hot_cue_slots.map((slot) => slot.slot), ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']);
		assert.ok(initial.hot_cue_slots.every((slot) => typeof slot.revision === 'string'));
		assert.equal(initial.hot_cue_reversal, null);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'hot_cue_save', deck: 1, slot: 'I', in_ms: 1000, revision: 'etag'
			}),
			/hot-cue slot must be A through H/i
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'hot_cue_restore', deck: 1, slot: 'A', revision: 'etag', reversal_id: 'token', extra: true
			}),
			/unexpected fields/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
	const [deckSource, ipcSource] = await Promise.all([
		readFile('src/lib/rb/deck-hot-cue-actions.ts', 'utf8'),
		readFile('src/lib/rb/performance-ipc.svelte.ts', 'utf8')
	]);
	assert.match(deckSource, /type: 'hot_cue_save' as const/);
	assert.match(deckSource, /type: 'hot_cue_restore', deck: deckId, slot, revision, reversal_id: reversalId/);
	assert.match(
		ipcSource,
		/await saveHotCue\(\s*stableId, command\.slot, savedPositionMs, command\.revision, command\.comment\s*\)/
	);
	assert.match(ipcSource, /await restoreHotCue\(stableId, command\.slot, command\.revision, command\.reversal_id\)/);
});

test('hot-cue IPC dispatch sends CAS revisions and exposes one-time reversal state', async () => {
	const originalFetch = globalThis.fetch;
	// This test is about CAS/reversal plumbing, not BeatSyncMax quantization,
	// and the stubbed deck below carries no real PQTZ beatgrid - but the
	// default preference (prefs.svelte.ts DEFAULTS.beat_sync_max) is true, and
	// hot_cue_save gates its quantize path on it, so an empty grid would throw
	// "beat grid must contain at least 2 beats" before this test ever reaches
	// the CAS assertions it actually cares about.
	const originalBeatSyncMax = ipc.uiPrefs.beat_sync_max;
	ipc.uiPrefs.beat_sync_max = false;
	const requests = [];
	let restoreCalls = 0;
	globalThis.fetch = async (input, init = {}) => {
		requests.push({ url: String(input), init });
		if (String(input).endsWith('/restore')) {
			restoreCalls += 1;
			if (restoreCalls > 1) {
				return Response.json(
					{ detail: { code: 'HOT_CUE_REVERSAL_CONSUMED', message: 'already used' } },
					{ status: 409 }
				);
			}
			return Response.json({ cue: null, revision: 'restore-revision' });
		}
		if (init.method === 'DELETE') {
			return Response.json({
				cue: null,
				revision: 'clear-revision',
				reversal: { reversal_id: 'clear-token' }
			});
		}
		return Response.json({
			cue: { slot: 'A', revision: 'save-revision' },
			revision: 'save-revision',
			reversal: { reversal_id: 'save-token' }
		});
	};
	globalThis.window = {};
	const resetDriver = ipc.installPerformanceHotCueDriverForTest({
		stableId: () => 'loaded-track',
		refresh: async () => {},
		hasRbMapping: () => true
	});
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const saved = await window.musicDjToolsPerformance.dispatch({
			type: 'hot_cue_save', deck: 1, slot: 'A', in_ms: 1000, revision: 'empty-revision'
		});
		assert.deepEqual(saved.decks[1].hot_cue_reversal, {
			slot: 'A', revision: 'save-revision', reversal_id: 'save-token'
		});
		await window.musicDjToolsPerformance.dispatch({
			type: 'hot_cue_clear', deck: 1, slot: 'A', revision: 'save-revision'
		});
		const restored = await window.musicDjToolsPerformance.dispatch({
			type: 'hot_cue_restore', deck: 1, slot: 'A', revision: 'clear-revision', reversal_id: 'clear-token'
		});
		assert.equal(restored.decks[1].hot_cue_reversal, null);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'hot_cue_restore', deck: 1, slot: 'A', revision: 'clear-revision', reversal_id: 'clear-token'
			}),
			/HOT_CUE_REVERSAL_CONSUMED/i
		);
		assert.equal(requests[0].init.headers['If-Match'], 'empty-revision');
		assert.equal(requests[1].init.headers['If-Match'], 'save-revision');
		assert.equal(requests[2].init.headers['If-Match'], 'clear-revision');
		assert.equal(requests[3].init.headers['If-Match'], 'clear-revision');
	} finally {
		uninstall();
		resetDriver();
		delete globalThis.window;
		globalThis.fetch = originalFetch;
		ipc.uiPrefs.beat_sync_max = originalBeatSyncMax;
	}
});

test('hot_cue_save rejects for an unmapped deck before reaching the network, same as HotCueBank (#736)', async () => {
	const originalFetch = globalThis.fetch;
	let fetchCalls = 0;
	globalThis.fetch = async () => {
		fetchCalls += 1;
		throw new Error('saveHotCue must not reach the network for an unmapped deck');
	};
	globalThis.window = {};
	const resetDriver = ipc.installPerformanceHotCueDriverForTest({
		stableId: () => 'loaded-track',
		refresh: async () => {},
		hasRbMapping: () => false
	});
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'hot_cue_save', deck: 1, slot: 'A', in_ms: 1000, revision: 'etag'
			}),
			/no live rekordbox mapping/i
		);
		assert.equal(fetchCalls, 0, 'hot_cue_save must reject before calling saveHotCue');
	} finally {
		uninstall();
		resetDriver();
		delete globalThis.window;
		globalThis.fetch = originalFetch;
	}
});

// Downbeats at 0.135 and 2.025, matching beat-sync-math.test.mjs's real PQTZ
// capture, so a wrong armAtPositionSec would surface as a wrong test number
// rather than an untestable magic value.
const TRIGGER_PQTZ_BEATS = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 },
	{ n: 1, bpm: 127, t: 2.025 },
	{ n: 2, bpm: 127, t: 2.497 },
	{ n: 3, bpm: 127, t: 2.97 },
	{ n: 4, bpm: 127, t: 3.442 }
];

function triggerDriverStub({ playing, loopEngaged, positionSec, contextTimeNowSec }) {
	const cue = { slot: 'A', in_ms: 4000, out_ms: null, is_loop: false, beat_loop_size: null, color_table_index: null, comment: null };
	const jumpCalls = [];
	const armCalls = [];
	let clockSec = contextTimeNowSec;
	return {
		driver: {
			stableId: () => 'loaded-track',
			refresh: async () => {},
			hasRbMapping: () => true,
			triggerState: () => ({ cue, playing, loopEngaged, positionSec, beats: TRIGGER_PQTZ_BEATS }),
			jump: async (deck, positionMs) => {
				jumpCalls.push({ deck, positionMs });
			},
			arm: async (deck, positionMs, armAtPositionSec) => {
				armCalls.push({ deck, positionMs, armAtPositionSec });
				const targetContextTime = clockSec + (armAtPositionSec - positionSec);
				return targetContextTime;
			},
			contextTimeNowSec: () => clockSec
		},
		jumpCalls,
		armCalls,
		advanceClock: (bySec) => {
			clockSec += bySec;
		}
	};
}

test('hot_cue_trigger jumps immediately unless BeatSyncMax, playing and unlooped all hold (#884)', async () => {
	globalThis.window = {};
	const cases = [
		{ playing: true, loopEngaged: false, beatSyncMax: false, label: 'BeatSyncMax off' },
		{ playing: false, loopEngaged: false, beatSyncMax: true, label: 'stopped deck' },
		{ playing: true, loopEngaged: true, beatSyncMax: true, label: 'engaged loop' }
	];
	const originalBeatSyncMax = ipc.uiPrefs.beat_sync_max;
	try {
		for (const { playing, loopEngaged, beatSyncMax, label } of cases) {
			ipc.uiPrefs.beat_sync_max = beatSyncMax;
			const { driver, jumpCalls, armCalls } = triggerDriverStub({
				playing,
				loopEngaged,
				positionSec: 1.8,
				contextTimeNowSec: 10
			});
			const resetDriver = ipc.installPerformanceHotCueDriverForTest(driver);
			const uninstall = ipc.installPerformanceBrowserIpc();
			try {
				const state = await window.musicDjToolsPerformance.dispatch({
					type: 'hot_cue_trigger', deck: 1, slot: 'A'
				});
				assert.deepEqual(jumpCalls, [{ deck: 1, positionMs: 4000 }], label);
				assert.deepEqual(armCalls, [], label);
				assert.equal(state.decks[1].hot_cue_armed, null, label);
			} finally {
				uninstall();
				resetDriver();
			}
		}
	} finally {
		delete globalThis.window;
		ipc.uiPrefs.beat_sync_max = originalBeatSyncMax;
	}
});

test('hot_cue_trigger arms for the next downbeat and exposes a live, self-expiring countdown (#884)', async () => {
	globalThis.window = {};
	const originalBeatSyncMax = ipc.uiPrefs.beat_sync_max;
	ipc.uiPrefs.beat_sync_max = true;
	const { driver, jumpCalls, armCalls, advanceClock } = triggerDriverStub({
		playing: true,
		loopEngaged: false,
		positionSec: 1.8,
		contextTimeNowSec: 10
	});
	const resetDriver = ipc.installPerformanceHotCueDriverForTest(driver);
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const state = await window.musicDjToolsPerformance.dispatch({
			type: 'hot_cue_trigger', deck: 1, slot: 'A'
		});
		// positionSec 1.8 -> next downbeat is 2.025, not the nearer-but-past one.
		assert.deepEqual(armCalls, [{ deck: 1, positionMs: 4000, armAtPositionSec: 2.025 }]);
		assert.deepEqual(jumpCalls, []);
		const armed = state.decks[1].hot_cue_armed;
		assert.equal(armed.slot, 'A');
		assert.equal(armed.target_position_ms, 4000);
		assert.ok(armed.remaining_ms > 0, 'a freshly-armed trigger reports a positive countdown');
		assert.equal(Math.round(armed.remaining_ms), 225, 'remaining_ms derives from the live clock, not a cached value');
		// The countdown is recomputed live, not cached: advancing the stubbed
		// clock changes the NEXT read without a new command.
		advanceClock(0.1);
		const midway = ipc.queryPerformanceState().decks[1].hot_cue_armed;
		assert.ok(midway.remaining_ms < armed.remaining_ms, 'remaining_ms must fall as the clock advances');
		// Once the target context time is reached, the armed record self-clears.
		advanceClock(1);
		const landed = ipc.queryPerformanceState().decks[1].hot_cue_armed;
		assert.equal(landed, null, 'an armed record must not linger once its target time has passed');
	} finally {
		uninstall();
		resetDriver();
		delete globalThis.window;
		ipc.uiPrefs.beat_sync_max = originalBeatSyncMax;
	}
});

test('hot_cue_trigger rejects an empty slot before touching jump or arm (#884)', async () => {
	globalThis.window = {};
	const { driver, jumpCalls, armCalls } = triggerDriverStub({
		playing: true,
		loopEngaged: false,
		positionSec: 1.8,
		contextTimeNowSec: 10
	});
	const resetDriver = ipc.installPerformanceHotCueDriverForTest({
		...driver,
		triggerState: () => ({ cue: null, playing: true, loopEngaged: false, positionSec: 1.8, beats: TRIGGER_PQTZ_BEATS })
	});
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'hot_cue_trigger', deck: 1, slot: 'B' }),
			/nothing to trigger/i
		);
		assert.deepEqual(jumpCalls, []);
		assert.deepEqual(armCalls, []);
	} finally {
		uninstall();
		resetDriver();
		delete globalThis.window;
	}
});

test('queryPerformanceState serializes has_rb_mapping so a browser/CLI agent can gate on it (#736)', () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const initial = ipc.queryPerformanceState().decks[1];
		assert.equal(initial.has_rb_mapping, true, 'a fresh unloaded deck defaults has_rb_mapping true');
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('deck header key controls dispatch only through the typed performance dispatcher', async () => {
	const deckSource = await readFile('src/lib/components/rb/Deck.svelte', 'utf8');
	const headerSource = await readFile('src/lib/components/rb/deck/DeckHeader.svelte', 'utf8');
	assert.match(
		deckSource,
		/runPerformanceCommandFromUi\(\{\s*type: 'key_sync',\s*deck: deckId,\s*enabled: !deck\.key_sync_enabled\s*\}\)/
	);
	assert.match(deckSource, /type: 'key_nudge', deck: deckId, semitones/);
	assert.match(headerSource, /onKeySync/);
	assert.match(headerSource, /onKeyNudge/);
	assert.match(headerSource, /aria-label=\{`lower key by one semitone deck \$\{deckId\}`\}/);
	assert.match(headerSource, /aria-label=\{`raise key by one semitone deck \$\{deckId\}`\}/);
	assert.match(deckSource, /candidate !== deckId/);
});

test('stem commands are strict typed IPC and default state never claims artifacts exist', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const state = ipc.queryPerformanceState();
		assert.equal(state.decks[1].stems.status, 'unavailable');
		assert.deepEqual(state.decks[1].stems.controls, {
			vocal: { muted: false, solo: false, gain: 0.5 },
			instrumental: { muted: false, solo: false, gain: 0.5 },
			drums: { muted: false, solo: false, gain: 0.5 }
		});

		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'stem_mute',
				deck: 1,
				stem: 'mix',
				muted: true
			}),
			/stem must be vocal, instrumental, or drums/i
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'stem_solo',
				deck: 1,
				stem: 'vocal',
				solo: 'yes'
			}),
			/solo must be boolean/i
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'stem_eq_mode',
				deck: 1,
				enabled: 'yes'
			}),
			/enabled must be boolean/i
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'stem_gain',
				deck: 1,
				stem: 'vocal',
				value: 1.5
			}),
			/value must be within 0..1/i
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'stem_gain',
				deck: 1,
				stem: 'mix',
				value: 0.5
			}),
			/stem must be vocal, instrumental, or drums/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('jog SLIP control dispatches only through the typed performance dispatcher', async () => {
	const deckSource = await readFile('src/lib/components/rb/Deck.svelte', 'utf8');
	const jogSource = await readFile('src/lib/components/rb/deck/JogDial.svelte', 'utf8');
	assert.match(deckSource, /type: 'slip',[\s\S]*enabled: !deck\.slip_enabled/);
	assert.match(jogSource, /onSlip/);
	assert.match(jogSource, /data-performance-control="slip"/);
});

test('jog live BPM uses PQTZ playbackBpm, not tag BPM times pitch', async () => {
	const jogSource = await readFile('src/lib/components/rb/deck/JogDial.svelte', 'utf8');
	assert.match(jogSource, /playbackBpm/);
	assert.doesNotMatch(
		jogSource,
		/deck\.bpm === null \? null : deck\.bpm \* deck\.pitch/
	);
});

test('continuous mixer controls execute through IPC immediately and round-trip in query state', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		for (const deckId of [1, 2, 3, 4]) {
			const channel = pairing.mixerState.channels[deckId];
			channel.trim = 0.5;
			channel.eq_high = 0.5;
			channel.eq_mid = 0.5;
			channel.eq_low = 0.5;
			channel.filter = 0.5;
			channel.fader = 1;
			channel.assign = deckId % 2 === 1 ? 'A' : 'B';
			channel.cue_enabled = false;
			channel.stem_eq_mode = false;
		}
		pairing.mixerState.crossfader = 0.5;
		pairing.mixerState.master = 1;

		await ipc.dispatchPerformanceCommand({ type: 'trim', deck: 2, value: 0.7 });
		await ipc.dispatchPerformanceCommand({ type: 'eq', deck: 2, band: 'mid', value: 0.25 });
		await ipc.dispatchPerformanceCommand({ type: 'filter', deck: 2, value: 0.9 });
		await ipc.dispatchPerformanceCommand({ type: 'fader', deck: 2, value: 0.8 });
		await ipc.dispatchPerformanceCommand({ type: 'assign', deck: 2, assign: 'THRU' });
		await ipc.dispatchPerformanceCommand({ type: 'channel_cue', deck: 2, enabled: true });
		await ipc.dispatchPerformanceCommand({ type: 'crossfader', value: 0.3 });
		await ipc.dispatchPerformanceCommand({ type: 'master_volume', value: 0.6 });
		await ipc.dispatchPerformanceCommand({ type: 'headphone_mix', value: 0.25 });
		await ipc.dispatchPerformanceCommand({ type: 'headphone_level', value: 0.75 });

		const state = ipc.queryPerformanceState();
		assert.equal(state.command_pending, false);
		assert.equal(state.command_queued, 0);
		assert.deepEqual(state.mixer, {
		crossfader: 0.3,
		master: 0.6,
		headphones: {
			mix: 0.25,
			level: 0.75,
			selected_output_device_id: null,
			selected_master_output_device_id: null,
			selected_input_device_id: null,
			output_mode: 'practice',
			head_delay_ms: 0,
			alignment_mode: 'hybrid',
			master_delay_ms: 0,
			calibration: {
				step: 'idle',
				cue_latency_ms: null,
				master_latency_ms: null,
				offset_ms: null,
				verify_residual_ms: null,
				probe: null,
				error: null
			},
			outputs: [],
			inputs: [],
			supported: false,
			active: false,
			error: null
		},
		channels: {
			1: {
				deck_id: 1,
				trim: 0.5,
				eq_high: 0.5,
				eq_mid: 0.5,
				eq_low: 0.5,
				filter: 0.5,
				fader: 1,
				assign: 'A',
				cue_enabled: false,
				stem_eq_mode: false
			},
			2: {
				deck_id: 2,
				trim: 0.7,
				eq_high: 0.5,
				eq_mid: 0.25,
				eq_low: 0.5,
				filter: 0.9,
				fader: 0.8,
				assign: 'THRU',
				cue_enabled: true,
				stem_eq_mode: false
			},
			3: {
				deck_id: 3,
				trim: 0.5,
				eq_high: 0.5,
				eq_mid: 0.5,
				eq_low: 0.5,
				filter: 0.5,
				fader: 1,
				assign: 'A',
				cue_enabled: false,
				stem_eq_mode: false
			},
			4: {
				deck_id: 4,
				trim: 0.5,
				eq_high: 0.5,
				eq_mid: 0.5,
				eq_low: 0.5,
				filter: 0.5,
				fader: 1,
				assign: 'B',
				cue_enabled: false,
				stem_eq_mode: false
			}
		}
		});

		await ipc.dispatchPerformanceCommand({ type: 'trim', deck: 2, value: 0.5 });
		await ipc.dispatchPerformanceCommand({ type: 'eq', deck: 2, band: 'mid', value: 0.5 });
		await ipc.dispatchPerformanceCommand({ type: 'filter', deck: 2, value: 0.5 });
		await ipc.dispatchPerformanceCommand({ type: 'fader', deck: 2, value: 1 });
		await ipc.dispatchPerformanceCommand({ type: 'assign', deck: 2, assign: 'B' });
		await ipc.dispatchPerformanceCommand({ type: 'channel_cue', deck: 2, enabled: false });
		await ipc.dispatchPerformanceCommand({ type: 'crossfader', value: 0.5 });
		await ipc.dispatchPerformanceCommand({ type: 'master_volume', value: 1 });
		await ipc.dispatchPerformanceCommand({ type: 'headphone_mix', value: 0.5 });
		await ipc.dispatchPerformanceCommand({ type: 'headphone_level', value: 0.5 });
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('mixer headphone controls use the typed dispatcher from every visible control', async () => {
	const [mixer, strip, headphones] = await Promise.all([
		readFile('src/lib/components/rb/Mixer.svelte', 'utf8'),
		readFile('src/lib/components/rb/mixer/ChannelStrip.svelte', 'utf8'),
		readFile('src/lib/components/rb/mixer/HeadphoneCluster.svelte', 'utf8')
	]);
	assert.match(mixer, /type: 'channel_cue'/);
	assert.match(mixer, /type: 'headphone_mix'/);
	assert.match(mixer, /type: 'headphone_level'/);
	assert.match(mixer, /type: 'head_delay_ms'/);
	assert.match(mixer, /type: 'headphone_outputs_refresh'/);
	assert.match(mixer, /type: 'headphone_output_acquire'/);
	assert.match(mixer, /type: 'headphone_output_select'/);
	assert.match(mixer, /type: 'headphone_master_select'/);
	assert.match(mixer, /type: 'headphone_input_select'/);
	// Pin 894af5672c3b: SET OUTPUTS records the session click, then acquires.
	assert.match(headphones, /function handleSetOutputs\(\): void \{[^}]*onacquire\(\);/);
	// bf60d7d67 feat(webui): pin master and cue sinks from I/O menu (#2409)
	// replaced the "+ OUT" grant button (title "Grant browser access to a second
	// audio output") with the I/O button, which still calls onacquire.
	assert.match(headphones, /aria-label="SHOW AUDIO I\/O"[^>]*onclick=\{handleSetOutputs\}/);
	assert.match(headphones, /I\/O briefly uses the built-in mic so device names appear/);
	assert.match(strip, /aria-pressed=\{cueEnabled\}/);
	assert.match(headphones, /aria-label="headphone output device"/);
	assert.match(headphones, /ondelay/);
});

// requirement: CUEOUT-01
// [if] output_mode is set outside the enum via IPC [then] the command throws and state is unchanged
test('output_mode IPC accepts split_cable and rejects unknown modes', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const before = ipc.queryPerformanceState().mixer.headphones;
		assert.equal(before.output_mode, 'practice');
		await ipc.dispatchPerformanceCommand({ type: 'output_mode', mode: 'split_cable' });
		assert.equal(ipc.queryPerformanceState().mixer.headphones.output_mode, 'split_cable');
		await ipc.dispatchPerformanceCommand({ type: 'output_mode', mode: 'practice' });
		assert.equal(ipc.queryPerformanceState().mixer.headphones.output_mode, 'practice');
		await ipc.dispatchPerformanceCommand({ type: 'output_mode', mode: 'split_cable' });
		assert.equal(ipc.queryPerformanceState().mixer.headphones.output_mode, 'split_cable');
		await ipc.dispatchPerformanceCommand({ type: 'output_mode', mode: 'two_outputs' });
		assert.equal(ipc.queryPerformanceState().mixer.headphones.output_mode, 'two_outputs');
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'output_mode', mode: 'nope' }),
			/split_cable/
		);
		assert.equal(ipc.queryPerformanceState().mixer.headphones.output_mode, 'two_outputs');
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('uninstall invalidates retained IPC dispatchers and a new route session remains usable', async () => {
	globalThis.window = {};
	pairing.mixerState.master = 1;
	try {
		const uninstallOldSession = ipc.installPerformanceBrowserIpc();
		const staleDispatch = window.musicDjToolsPerformance.dispatch;
		uninstallOldSession();

		await assert.rejects(
			staleDispatch({ type: 'master_volume', value: 0.2 }),
			/performance command session .* invalidated/i
		);
		assert.equal(ipc.queryPerformanceState().mixer.master, 1);

		const uninstallNewSession = ipc.installPerformanceBrowserIpc();
		await window.musicDjToolsPerformance.dispatch({ type: 'master_volume', value: 0.4 });
		const state = ipc.queryPerformanceState();
		assert.equal(state.mixer.master, 0.4);
		assert.equal(state.command_pending, false);
		assert.equal(state.command_queued, 0);
		await window.musicDjToolsPerformance.dispatch({ type: 'master_volume', value: 1 });
		uninstallNewSession();
	} finally {
		delete globalThis.window;
	}
});

test('uninstall cannot invalidate a session after browser IPC ownership changes', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	const ownedIpc = window.musicDjToolsPerformance;
	const foreignIpc = { version: 1 };
	window.musicDjToolsPerformance = foreignIpc;
	try {
		assert.throws(uninstall, /ownership changed before cleanup/i);
		assert.equal(window.musicDjToolsPerformance, foreignIpc);
		await ownedIpc.dispatch({ type: 'master_volume', value: 0.4 });
		assert.equal(ipc.queryPerformanceState().mixer.master, 0.4);
		await ownedIpc.dispatch({ type: 'master_volume', value: 1 });
	} finally {
		window.musicDjToolsPerformance = ownedIpc;
		uninstall();
		delete globalThis.window;
	}
});

//-----------------------------------------------------------------------------
// agent-native parity for the toast tray
//-----------------------------------------------------------------------------

/**
 * Every pointer interaction the toast tray offers must have an equal here, or
 * an agent cannot drive and assert the same flows a human can.
 *
 * Behavior lives in toast-behavior.test.mjs against the store; what is pinned
 * here is that the bridge EXPOSES it, validates its input like every other IPC
 * entry point, and delegates rather than reimplementing.
 *
 * Regression lines:
 * - if a tray method disappears from the IPC then agent parity silently lapses
 *   and only a human can dismiss a stuck toast
 * - if an id reaches a store lookup unvalidated then the untyped bridge is the
 *   one input path that skips the validation every other one performs
 * - if a method reimplements the behavior instead of delegating then the pointer
 *   and the agent can drift apart
 */
test('the IPC exposes an equal for every toast interaction a pointer has', () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const bridge = globalThis.window.musicDjToolsPerformance;
		for (const method of ['toasts', 'dismissToast', 'holdToast', 'releaseToast', 'copyToast']) {
			assert.equal(typeof bridge[method], 'function', `${method} must be reachable by an agent`);
		}
		assert.ok(Array.isArray(bridge.toasts()), 'toasts() lists what is on screen');
	} finally {
		uninstall();
	}
});

test('a toast id arriving over the untyped bridge is validated, not trusted', () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const bridge = globalThis.window.musicDjToolsPerformance;
		for (const method of ['dismissToast', 'holdToast', 'releaseToast']) {
			for (const bad of [undefined, null, 42, '', {}]) {
				assert.throws(
					() => bridge[method](bad),
					TypeError,
					`${method} must refuse ${JSON.stringify(bad) ?? 'undefined'}`
				);
			}
		}
	} finally {
		uninstall();
	}
});

test('dismissing a toast is NOT gated on the performance command session', async () => {
	// A toast outlives any one command session. Gating it would mean a preset
	// transaction moving on could leave a stuck error undismissable, which is
	// the bug the dismiss control exists to fix.
	const source = await readFile(
		new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url),
		'utf8'
	);
	const block = source.slice(source.indexOf('dismissToast: (id: unknown)'));
	const nextMethod = block.indexOf('copyToast:');
	assert.equal(
		/_assertCommandSession/.test(block.slice(0, nextMethod)),
		false,
		'no toast method may sit behind the command-session gate'
	);
	assert.match(
		source,
		/dismissToast: \(id: unknown\) => dismissToast\(_toastId\(id\)\)/,
		'the bridge must delegate to the store, not reimplement dismissal'
	);
	assert.match(source, /copyToast: \(id: unknown\) => copyToast\(_toastId\(id\)\)/);
});

test('load play intent is strictly validated, immediate, and visible to agents', async () => {
	const source = await readFile(
		new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url),
		'utf8'
	);
	assert.match(source, /type: 'load_play_intent'; deck: DeckId; generation: number; desired_play: boolean/);
	assert.match(source, /_exactKeys\(record, \['type', 'deck', 'generation', 'desired_play'\]\)/);
	assert.match(source, /generation must be a positive safe integer/);
	// Any number of sibling types may share the load_play_intent early-return group; a fixed
	// character window broke when PREF-02 appended feedback_mark to it (main red, Fri 11 Sep 2026).
	assert.match(source, /command\.type === 'load_play_intent'(?:\s*\|\| command\.type === '[a-z_]+')*\s*\)\s*\{\s*return null;/);
	assert.match(source, /load_play_intent: Record<DeckId, \{ generation: number; desired_play: boolean \} \| null>/);
});

test('queryPerformanceState publishes browser pane search, sort, and selected row from the adapter', () => {
	const empty = ipc.queryPerformanceState().browser;
	assert.equal(empty.search, null);
	assert.equal(empty.sort, null);
	assert.equal(empty.selected_row, null);

	const unregister = ipc.registerPerformanceBrowserAdapter({
		selectPlaylist: async () => {},
		readSnapshot: () => ({
			search: 'house',
			sort: { key: 'title', direction: 'asc' },
			selected_row: 'track-99'
		})
	});
	try {
		const live = ipc.queryPerformanceState().browser;
		assert.equal(live.search, 'house');
		assert.deepEqual(live.sort, { key: 'title', direction: 'asc' });
		assert.equal(live.selected_row, 'track-99');
		assert.doesNotThrow(() => structuredClone(live));
	} finally {
		unregister();
	}

	const unregisterDesc = ipc.registerPerformanceBrowserAdapter({
		selectPlaylist: async () => {},
		readSnapshot: () => ({
			search: null,
			sort: { key: 'bpm', direction: 'desc' },
			selected_row: null
		})
	});
	try {
		assert.deepEqual(ipc.queryPerformanceState().browser.sort, {
			key: 'bpm',
			direction: 'desc'
		});
	} finally {
		unregisterDesc();
	}

	const after = ipc.queryPerformanceState().browser;
	assert.equal(after.search, null);
	assert.equal(after.sort, null);
	assert.equal(after.selected_row, null);
});

test('master mute and browser playlist selection are bus commands with queryable state', async () => {
	const seen = [];
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	const unregister = ipc.registerPerformanceBrowserAdapter({
		selectPlaylist: async (playlistId) => {
			seen.push(playlistId);
		},
		readSnapshot: () => ({ search: null, sort: null, selected_row: null })
	});
	try {
		const before = ipc.queryPerformanceState().history.length;
		const muted = await ipc.dispatchPerformanceCommand({ type: 'master_mute', muted: true });
		assert.equal(muted.master.muted, true);
		assert.equal(muted.history.at(-1).type, 'master_mute');
		const observed = await ipc.dispatchPerformanceCommand({ type: 'pitch_range', deck: 1, range: 8 });
		assert.equal(observed.decks[1].last_command_id, observed.history.at(-1).id);

		const selected = await ipc.dispatchPerformanceCommand({
			type: 'browser_select_playlist',
			playlist_id: 'playlist-42'
		});
		assert.deepEqual(seen, ['playlist-42']);
		assert.equal(selected.browser.active_playlist, 'playlist-42');
		assert.equal(selected.history.length, before + 3);
	} finally {
		unregister();
		uninstall();
		delete globalThis.window;
	}
});

// ----- pin 88e3abec02a0: "show other users' pins" is stubbed, not silent --
// The community-pins toggle in FeedbackWidget is inert (rb-inert, disabled),
// but per AGENT-NATIVE PARITY every UI control still needs a matching
// performance-bus command - one that answers honestly rather than pretending
// to work. Modelled on auto_play_two_track, the existing precedent for a
// UI-contract-only command that is rejected at the dispatch boundary before
// it ever reaches deck/engine logic.
test('pins_show_other_users is a registered performance command that refuses not_implemented', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			() => ipc.dispatchPerformanceCommand({ type: 'pins_show_other_users' }),
			/not_implemented/
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

// PARITY-02, discussion_r3968214027 P1 BLOCKING: the SOURCE toggle is a real UI
// control whose whole point is A/B testing rbx-vs-own, and every UI action needs
// an agent-native counterpart. The WRITE half existed (the `analysis_source`
// command), but an agent reading queryPerformanceState() saw no field for it, so
// it could switch the source and never confirm which one was in effect - and
// could not attribute a beatgrid/BPM readback to a lane at all.
test('queryPerformanceState publishes transition from readTransition and defaults to idle', () => {
	const source = readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	assert.match(source, /transition:\s*readTransition\(\)/);
	assert.equal(ipc.queryPerformanceState().transition.state, 'idle');
	assert.doesNotThrow(() => structuredClone(ipc.queryPerformanceState().transition));
});

test('queryPerformanceState reports the analysis source selection, as a snapshot not the live rune', () => {
	pairing.analysisSourceState.features = { beatgrid: 'own' };

	const state = pairing.queryPerformanceState();

	assert.deepEqual(state.analysis_source, { beatgrid: 'own' });
	// Real Svelte 5 wraps a $state object in a Proxy and structuredClone throws
	// DataCloneError on one (see performance-ipc-pairing-clone.test.mjs), so the
	// field has to be a rebuilt plain object. Identity is the check that bites
	// under the test loader, where $state is an identity function.
	assert.notEqual(
		state.analysis_source,
		pairing.analysisSourceState.features,
		'handing back the live rune sends a Proxy across the IPC boundary'
	);
	assert.doesNotThrow(() => structuredClone(state.analysis_source));

	state.analysis_source.beatgrid = 'rekordbox';
	assert.equal(
		pairing.analysisSourceState.features.beatgrid,
		'own',
		'an IPC consumer mutating its own snapshot must not write back into the toggle'
	);
});

// REQ: LATENCY-02
test('LATENCY-02 play.quantize arms countdown and plain play cancels while armed', async () => {
	globalThis.window = {};
	ipc.resetQuantizedLaunchArmedForTest();
	const armCalls = [];
	const clearCalls = [];
	let clockSec = 10;
	const resetDriver = ipc.installPerformanceQuantizedLaunchDriverForTest({
		arm: async (...args) => {
			armCalls.push(args);
			return 12.25;
		},
		clear: (deck) => {
			clearCalls.push(deck);
		},
		contextTimeNowSec: () => clockSec
	});
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await window.musicDjToolsPerformance.dispatch({
			type: 'play',
			deck: 2,
			playing: true,
			quantize: true
		});
		assert.equal(armCalls.length, 1);
		const armed = ipc.queryPerformanceState().decks[2].quantized_launch_armed;
		assert.notEqual(armed, null);
		assert.ok(armed.remaining_ms > 0);
		await window.musicDjToolsPerformance.dispatch({ type: 'play', deck: 2, playing: true });
		assert.equal(clearCalls.length, 1);
		assert.equal(ipc.queryPerformanceState().decks[2].quantized_launch_armed, null);
		clockSec = 12.26;
		assert.equal(ipc.queryPerformanceState().decks[2].quantized_launch_armed, null);
	} finally {
		uninstall();
		resetDriver();
		ipc.resetQuantizedLaunchArmedForTest();
		delete globalThis.window;
	}
});

test('LATENCY-02 play parse accepts omitted quantize and rejects unknown fields', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'play',
				deck: 1,
				playing: true,
				extra: true
			}),
			/unexpected fields/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('RESCUE-02 rescue_resume and rescue_stop_all parse, scope all decks, and lock play during restore', async () => {
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({
			type: 'rescue_resume',
			decks: [{ deck: 1, position_ms: 5000 }, { deck: 3, position_ms: 12_000 }]
		}),
		ipc.PERFORMANCE_RESCUE_COMMAND_SCOPES
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'rescue_stop_all' }),
		ipc.PERFORMANCE_RESCUE_COMMAND_SCOPES
	);

	ipc.setRescueRestorePhaseForTest('restoring');
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'load', deck: 1, stable_id: 'track-a' }),
			/rescue restore owns controls/
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'rescue_resume', decks: [] }),
			/requires at least one deck/
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'rescue_stop_all', extra: true }),
			/unexpected fields/i
		);
	} finally {
		ipc.setRescueRestorePhaseForTest('idle');
		uninstall();
		delete globalThis.window;
	}
});

test('master command accepts optional lock and query exposes master_mode', async () => {
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'master', deck: 4, lock: true }),
		[4, 'sync']
	);
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'master', deck: 1, lock: true, extra: true }),
			/unexpected fields/i
		);
		const state = ipc.queryPerformanceState();
		assert.equal(state.master_mode, 'auto');
		assert.equal(state.master_reason, null);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});
