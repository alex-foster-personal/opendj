// requirement: PLAY-11 (issue #3532)
// [if] AutoPlay Next aborts [then] the outgoing loop is released, [else stop].
// [if] approximateDropMs is null after bass entry [then] loop releases without waiting for position, [else stop].
// [if] the outgoing deck unloads while armed [then] the loop is released, [else stop].
import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STUB_IPC = fileURLToPath(
	new URL('./fixtures/auto-play-next-stub-ipc.ts', import.meta.url)
);

let entry;

function _beats(n) {
	const out = [];
	for (let i = 0; i < n; i++) out.push({ n: (i % 4) + 1, bpm: 128, t: i * 0.469 });
	return out;
}

function _flatWaveform(length) {
	return {
		kind: 'tri',
		preview: { length: 1, low: [0], mid: [0], high: [0] },
		detail: {
			length,
			low: new Array(length).fill(0.4),
			mid: new Array(length).fill(0.3),
			high: new Array(length).fill(0.2)
		}
	};
}

function _setupLoadedDecks(harness, { incomingBassAtDetailIndex = 500 } = {}) {
	const beats = _beats(32);
	const durationMs = (beats[beats.length - 1].t + 2) * 1000;
	const outgoingWaveform = _flatWaveform(3200);
	const incomingLow = new Array(3200).fill(0.05);
	for (let i = incomingBassAtDetailIndex; i < incomingLow.length; i++) incomingLow[i] = 0.9;
	const incomingWaveform = {
		kind: 'tri',
		preview: { length: 1, low: [0], mid: [0], high: [0] },
		detail: {
			length: 3200,
			low: incomingLow,
			mid: new Array(3200).fill(0.3),
			high: new Array(3200).fill(0.2)
		}
	};
	const outgoingAnlz = { waveform: outgoingWaveform, beatgrid: { beats } };
	const incomingAnlz = { waveform: incomingWaveform, beatgrid: { beats } };
	for (const deckId of [1, 2, 3, 4]) {
		const deck = harness.deckStates[deckId];
		deck.stable_id = null;
		deck.playing = false;
		deck.is_master = false;
		deck.anlz = null;
		deck.duration_ms = null;
		deck.position_ms = 0;
		deck.loop = null;
	}
	harness.deckStates[1].stable_id = 'outgoing-track';
	harness.deckStates[1].playing = true;
	harness.deckStates[1].is_master = true;
	harness.deckStates[1].anlz = outgoingAnlz;
	harness.deckStates[1].duration_ms = durationMs;
	harness.deckStates[2].stable_id = 'incoming-track';
	harness.deckStates[2].anlz = incomingAnlz;
	harness.deckStates[2].duration_ms = durationMs;
	harness.deckStates[2].position_ms = 0;
	return { beats, durationMs };
}

async function _pollUntilFirstBeatLoop(harness, maxTurns = 50) {
	for (let i = 0; i < maxTurns; i += 1) {
		if (harness.getDispatchPerformanceCommandCalls().some((cmd) => cmd.type === 'beat_loop')) {
			return;
		}
		await Promise.resolve();
	}
	throw new Error('beat_loop dispatch never recorded');
}

function _holdFirstBeatLoopStub(harness) {
	let releaseBeatLoop;
	const heldBeatLoop = new Promise((resolve) => {
		releaseBeatLoop = resolve;
	});
	let beatLoopCount = 0;
	harness.setDispatchPerformanceCommandStub(async (cmd) => {
		if (cmd.type === 'beat_loop') {
			beatLoopCount += 1;
			if (beatLoopCount === 1) await heldBeatLoop;
		}
	});
	return { releaseBeatLoop };
}

before(async () => {
	entry = await loadTypeScriptModule('tests/unit/fixtures/auto-play-next-entry.ts', {
		alias: { '$lib/rb/performance-ipc.svelte': STUB_IPC }
	});
});

beforeEach(() => {
	entry.resetDispatchPerformanceCommandStub();
	entry.setDispatchPerformanceCommandStub(async () => {});
});

afterEach(async () => {
	await entry.cancelAutoPlayNext();
	entry.resetDispatchPerformanceCommandStub();
});

test('arm dispatches beat_loop on a downbeat instead of raw loop', async () => {
	const { beats } = _setupLoadedDecks(entry);
	const armed = await entry.armAutoPlayNext();
	assert.equal(armed, true);
	const beatLoop = entry
		.getDispatchPerformanceCommandCalls()
		.find((cmd) => cmd.type === 'beat_loop');
	assert.ok(beatLoop, 'expected beat_loop dispatch');
	assert.equal(beatLoop.deck, 1);
	assert.equal(beatLoop.beats, 8);
	const startIdx = beats.findIndex((beat) => beat.t * 1000 === beatLoop.start_ms);
	assert.ok(startIdx >= 0);
	assert.equal(beats[startIdx].n, 1);
	assert.ok(startIdx + 8 < beats.length);
	assert.equal(
		entry.getDispatchPerformanceCommandCalls().some((cmd) => cmd.type === 'loop' && cmd.loop !== null),
		false,
		'raw loop engage must not be used'
	);
});

test('cancelAutoPlayNext releases the outgoing loop exactly once', async () => {
	_setupLoadedDecks(entry);
	assert.equal(await entry.armAutoPlayNext(), true);
	await entry.cancelAutoPlayNext();
	const releases = entry
		.getDispatchPerformanceCommandCalls()
		.filter((cmd) => cmd.type === 'loop' && cmd.loop === null);
	assert.equal(releases.length, 1);
	assert.equal(releases[0].deck, 1);
	assert.equal(entry.autoPlayNextState.armed, false);
});

test('approximateDropMs null path releases the loop without waiting for incoming position', async () => {
	const { beats, durationMs } = _setupLoadedDecks(entry, { incomingBassAtDetailIndex: 0 });
	assert.equal(await entry.armAutoPlayNext(), true);
	entry.deckStates[2].position_ms = beats[beats.length - 2].t * 1000;
	await new Promise((resolve) => setTimeout(resolve, 300));
	const releases = entry
		.getDispatchPerformanceCommandCalls()
		.filter((cmd) => cmd.type === 'loop' && cmd.loop === null);
	assert.ok(releases.length >= 1, 'loop must release when drop cannot be approximated');
	assert.equal(entry.autoPlayNextState.armed, false);
	assert.ok(durationMs > 0);
});

test('outgoing deck unload releases the engaged loop', async () => {
	_setupLoadedDecks(entry);
	assert.equal(await entry.armAutoPlayNext(), true);
	entry.deckStates[1].stable_id = null;
	await new Promise((resolve) => setTimeout(resolve, 300));
	const releases = entry
		.getDispatchPerformanceCommandCalls()
		.filter((cmd) => cmd.type === 'loop' && cmd.loop === null);
	assert.ok(releases.length >= 1);
	assert.equal(entry.autoPlayNextState.armed, false);
});

// requirement: AGENT-48
function _planner(harness, answer) {
	const calls = { scheduled: [], aborted: [], requests: [], played: [] };
	const state = { status: { id: 'plan-1', status: 'scheduled' } };
	const holdSlots = [];
	const queueHolds = answer.holds === 'queue';
	const unregister = harness.registerAutoPlayNextPlanRunner({
		schedule: async (plan) => {
			calls.scheduled.push(plan);
			if (queueHolds && plan && typeof plan.id === 'string') {
				state.status = { id: plan.id, status: 'scheduled' };
			}
			return { failed: answer.refuse ?? null };
		},
		abort: async (id) => {
			calls.aborted.push(id);
		},
		status: () => state.status
	});
	const realFetch = globalThis.fetch;
	globalThis.fetch = async (url, init) => {
		if (String(url).endsWith('/pair-transition/played')) {
			calls.played.push(JSON.parse(init.body));
			return new Response('{}', { status: 200 });
		}
		calls.requests.push(JSON.parse(init.body));
		if (queueHolds) {
			await new Promise((resolve) => {
				holdSlots.push(resolve);
			});
		} else if (answer.hold) {
			await answer.hold;
		}
		const reqN = calls.requests.length;
		const planId = queueHolds ? `plan-${reqN}` : 'plan-1';
		return new Response(
			JSON.stringify({
				order: answer.noOrder ? null : { plan: { id: planId } },
				reason: answer.noOrder ? 'nothing eligible' : 'intent smooth',
				intent: 'smooth',
				pick: answer.noOrder
					? null
					: { candidate: 'c1', style: 'long_blend', bars: 32, score: 0.4, out_start_bar: 9, rationale: 'r' },
				out_bar_now: 1,
				considered: 4,
				rendered: 4,
				propose_ms: 1,
				render_ms: 1,
				scored: true,
				plan_ms: 1,
				saved: answer.saved === true,
				plan: { id: 'doc' }
			}),
			{ status: 200 }
		);
	};
	return {
		calls,
		state,
		/** Release the hold for planner request `index` (0 = first fetch) without releasing earlier holds. */
		releaseHold: (index) => {
			const resolve = holdSlots[index];
			if (typeof resolve === 'function') {
				holdSlots[index] = null;
				resolve();
			}
		},
		restore: () => {
			unregister();
			globalThis.fetch = realFetch;
		}
	};
}

test('a planned answer runs on the plan runner, not the loop, and its style joins the variety memory', async () => {
	_setupLoadedDecks(entry);
	const planner = _planner(entry, {});
	try {
		assert.equal(await entry.armAutoPlayNext({ intent: 'energy_up' }), true);
		assert.deepEqual(planner.calls.scheduled, [{ id: 'plan-1' }]);
		assert.equal(planner.calls.requests[0].intent, 'energy_up');
		assert.equal(entry.autoPlayNextState.phase, 'planned');
		assert.equal(entry.autoPlayNextState.run.path, 'planner');
		assert.equal(entry.getDispatchPerformanceCommandCalls().some((cmd) => cmd.type === 'beat_loop'), false);
		planner.state.status = { id: 'plan-1', status: 'done' };
		await new Promise((resolve) => setTimeout(resolve, 300));
		assert.equal(entry.autoPlayNextState.armed, false);
		assert.equal(entry.autoPlayNextState.run.style, 'long_blend', 'the readout outlives the arm');
		assert.equal(await entry.armAutoPlayNext(), true);
		assert.ok(planner.calls.requests[1].recent_styles.includes('long_blend'));
	} finally {
		planner.state.status = { id: 'plan-1', status: 'done' };
		await entry.cancelAutoPlayNext();
		planner.restore();
	}
});

test('no eligible pick and a refused plan both fall back to the loop and say why', async () => {
	_setupLoadedDecks(entry);
	let planner = _planner(entry, { noOrder: true });
	try {
		assert.equal(await entry.armAutoPlayNext(), true);
		assert.equal(entry.autoPlayNextState.run.path, 'featurette');
		assert.equal(entry.autoPlayNextState.run.reason, 'nothing eligible');
		assert.ok(entry.getDispatchPerformanceCommandCalls().some((cmd) => cmd.type === 'beat_loop'));
		await entry.cancelAutoPlayNext();
	} finally {
		planner.restore();
	}
	planner = _planner(entry, { refuse: 'no_grid: deck 1 has no downbeats' });
	try {
		assert.equal(await entry.armAutoPlayNext(), true);
		assert.equal(entry.autoPlayNextState.run.path, 'featurette');
		assert.match(entry.autoPlayNextState.run.reason, /plan refused: no_grid/);
	} finally {
		planner.restore();
	}
});

test('cancel while the fallback beat_loop is pending releases the loop and stays disarmed', async () => {
	// requirement: AGENT-8383
	// [if] cancel arrives while fallback beat_loop is pending [then] the late loop is released and armed is false, [else stop].
	_setupLoadedDecks(entry);
	const { releaseBeatLoop } = _holdFirstBeatLoopStub(entry);
	const arming = entry.armAutoPlayNext();
	await _pollUntilFirstBeatLoop(entry);
	assert.equal(entry.autoPlayNextState.phase, 'planning');
	assert.equal(entry.autoPlayNextState.armed, true);
	await entry.cancelAutoPlayNext();
	releaseBeatLoop();
	assert.equal(await arming, false);
	const loopRelease = entry
		.getDispatchPerformanceCommandCalls()
		.find((cmd) => cmd.type === 'loop' && cmd.loop === null && cmd.deck === 1);
	assert.ok(loopRelease, 'expected loop: null on outgoing deck after stale beat_loop');
	assert.equal(entry.autoPlayNextState.armed, false);
	assert.equal(entry.autoPlayNextState.phase, 'idle');
	assert.equal(entry.autoPlayNextState.outgoing, null);
	assert.equal(entry.autoPlayNextState.incoming, null);
});

test('a cancelled fallback does not reset a later arm', async () => {
	// requirement: AGENT-8383
	// [if] a cancelled fallback later returns false [then] a newer arm's state is left intact, [else stop].
	_setupLoadedDecks(entry);
	const { releaseBeatLoop } = _holdFirstBeatLoopStub(entry);
	const firstArming = entry.armAutoPlayNext();
	await _pollUntilFirstBeatLoop(entry);
	await entry.cancelAutoPlayNext();
	const secondArming = entry.armAutoPlayNext();
	assert.equal(await secondArming, true);
	assert.equal(entry.autoPlayNextState.armed, true);
	assert.equal(entry.autoPlayNextState.phase, 'looping');
	releaseBeatLoop();
	assert.equal(await firstArming, false);
	assert.equal(entry.autoPlayNextState.armed, true);
	assert.equal(entry.autoPlayNextState.phase, 'looping');
	assert.equal(entry.autoPlayNextState.outgoing, 1);
	const calls = entry.getDispatchPerformanceCommandCalls();
	const beatLoopIdx = calls.findLastIndex((cmd) => cmd.type === 'beat_loop');
	assert.ok(beatLoopIdx >= 0);
	const trailingNull = calls.slice(beatLoopIdx + 1).some((cmd) => cmd.type === 'loop' && cmd.loop === null);
	assert.equal(trailingNull, false, 'stale fallback must not strip the second arm loop');
});

test('cancel while planning drops the late answer: no plan, no loop', async () => {
	_setupLoadedDecks(entry);
	let release;
	const hold = new Promise((resolve) => {
		release = resolve;
	});
	const planner = _planner(entry, { hold });
	try {
		const arming = entry.armAutoPlayNext();
		await Promise.resolve();
		assert.equal(entry.autoPlayNextState.phase, 'planning');
		await entry.cancelAutoPlayNext();
		release();
		assert.equal(await arming, false);
		assert.deepEqual(planner.calls.scheduled, []);
		assert.equal(entry.getDispatchPerformanceCommandCalls().length, 0);
		assert.equal(entry.autoPlayNextState.armed, false);
	} finally {
		planner.restore();
	}
});

test('stale planner completion cannot replace a newer handoff plan', async () => {
	// requirement: AGENT-48
	// [if] arm1 planner pending then cancel and arm2 plans [then] arm1 resolving last does not replace handoff, [else stop].
	_setupLoadedDecks(entry);
	const planner = _planner(entry, { holds: 'queue' });
	try {
		const arm1 = entry.armAutoPlayNext({ intent: 'smooth' });
		await Promise.resolve();
		assert.equal(entry.autoPlayNextState.phase, 'planning');
		await entry.cancelAutoPlayNext();
		assert.equal(entry.autoPlayNextState.armed, false);
		const arm2 = entry.armAutoPlayNext({ intent: 'energy_up' });
		assert.equal(planner.calls.requests.length, 2);
		planner.releaseHold(1);
		assert.equal(await arm2, true);
		assert.equal(planner.calls.scheduled.length, 1);
		assert.equal(entry.autoPlayNextState.phase, 'planned');
		assert.equal(entry.autoPlayNextState.run.path, 'planner');
		assert.equal(planner.calls.requests[1].intent, 'energy_up');
		assert.deepEqual(planner.calls.scheduled[0], { id: 'plan-2' });
		const livePlanId = 'plan-2';
		planner.releaseHold(0);
		assert.equal(await arm1, false);
		assert.equal(planner.calls.scheduled.length, 1, 'stale arm must not schedule again');
		assert.equal(entry.autoPlayNextState.armed, true);
		assert.equal(entry.autoPlayNextState.phase, 'planned');
		assert.equal(entry.autoPlayNextState.run.style, 'long_blend');
		assert.equal(
			planner.calls.aborted.includes(livePlanId),
			false,
			'stale completion must not abort the live plan'
		);
	} finally {
		planner.restore();
		await entry.cancelAutoPlayNext();
	}
});

test('cancel of a planned transition aborts that plan', async () => {
	_setupLoadedDecks(entry);
	const planner = _planner(entry, {});
	try {
		assert.equal(await entry.armAutoPlayNext(), true);
		await entry.cancelAutoPlayNext();
		assert.deepEqual(planner.calls.aborted, ['plan-1']);
		assert.equal(entry.autoPlayNextState.armed, false);
	} finally {
		planner.restore();
	}
});

test('a fallback that throws never leaves the button armed', async () => {
	_setupLoadedDecks(entry);
	entry.deckStates[1].anlz = { ...entry.deckStates[1].anlz, beatgrid: { beats: [] } };
	const planner = _planner(entry, { noOrder: true });
	try {
		await assert.rejects(entry.armAutoPlayNext(), /beats must be non-empty/);
		assert.equal(entry.autoPlayNextState.armed, false);
		assert.equal(entry.autoPlayNextState.phase, 'idle');
		assert.equal(entry.autoPlayNextState.run.reason, 'nothing eligible');
	} finally {
		planner.restore();
	}
});

test('cancel while the runner is taking the plan aborts the plan it armed', async () => {
	_setupLoadedDecks(entry);
	const planner = _planner(entry, {});
	let take;
	const taking = new Promise((resolve) => {
		take = resolve;
	});
	const unregister = entry.registerAutoPlayNextPlanRunner({
		schedule: async () => {
			await taking;
			return { failed: null };
		},
		abort: async (id) => {
			planner.calls.aborted.push(id);
		},
		status: () => planner.state.status
	});
	try {
		const arming = entry.armAutoPlayNext();
		while (entry.autoPlayNextState.run === null) await new Promise((resolve) => setTimeout(resolve, 5));
		await entry.cancelAutoPlayNext();
		take();
		assert.equal(await arming, false);
		assert.deepEqual(planner.calls.aborted, ['plan-1']);
		assert.equal(entry.autoPlayNextState.armed, false);
	} finally {
		unregister();
		planner.restore();
	}
});

// requirement: AGENT-50
test('a named point plans between the named decks, takes a playing deck, and never falls back to the loop', async () => {
	_setupLoadedDecks(entry);
	entry.deckStates[3].stable_id = 'third-track';
	entry.deckStates[3].playing = true;
	entry.deckStates[3].duration_ms = 200_000;
	let planner = _planner(entry, {});
	try {
		assert.equal(await entry.armAutoPlayNext({ outDeck: 1, inDeck: 3, fromMs: 61_000 }), true);
		const sent = planner.calls.requests[0];
		assert.equal(sent.out_deck, 1);
		assert.equal(sent.in_deck, 3);
		assert.equal(sent.from_ms, 61_000);
		assert.equal(sent.take_incoming, true);
		const stops = entry.getDispatchPerformanceCommandCalls().filter((cmd) => cmd.type === 'play' && cmd.deck === 3);
		assert.deepEqual(stops.map((cmd) => cmd.playing), [false], 'the taken deck is stopped once, before the plan');
		await entry.cancelAutoPlayNext();
	} finally {
		planner.restore();
	}
	entry.resetDispatchPerformanceCommandStub();
	entry.setDispatchPerformanceCommandStub(async () => {});
	planner = _planner(entry, { noOrder: true });
	try {
		assert.equal(await entry.armAutoPlayNext({ outDeck: 1, inDeck: 2, fromMs: 61_000 }), false);
		assert.equal(entry.autoPlayNextState.run.path, 'none');
		assert.match(entry.autoPlayNextState.run.reason, /no planned transition here: nothing eligible/);
		assert.equal(entry.getDispatchPerformanceCommandCalls().some((cmd) => cmd.type === 'beat_loop'), false);
		assert.equal(entry.autoPlayNextState.armed, false);
		// Control: the >| button with no named point still falls back to the loop.
		assert.equal(await entry.armAutoPlayNext(), true);
		assert.ok(entry.getDispatchPerformanceCommandCalls().some((cmd) => cmd.type === 'beat_loop'));
	} finally {
		planner.restore();
	}
});

test('a played-out or empty deck is no transition target, a playing one is', () => {
	_setupLoadedDecks(entry);
	assert.equal(entry.transitionTargetOk(2), true);
	assert.equal(entry.transitionTargetOk(4), false);
	entry.deckStates[2].position_ms = entry.deckStates[2].duration_ms;
	assert.equal(entry.transitionTargetOk(2), false);
	entry.deckStates[2].playing = true;
	assert.equal(entry.transitionTargetOk(2), true);
});

test('>| never takes a playing deck, and a failed plan gives a taken deck back', async () => {
	_setupLoadedDecks(entry);
	entry.deckStates[2].playing = true;
	let planner = _planner(entry, {});
	try {
		assert.equal(await entry.armAutoPlayNext(), true);
		assert.equal(planner.calls.requests[0].take_incoming, undefined);
		assert.equal(entry.getDispatchPerformanceCommandCalls().some((cmd) => cmd.type === 'play'), false);
		await entry.cancelAutoPlayNext();
	} finally {
		planner.restore();
	}
	planner = _planner(entry, { refuse: 'no_grid' });
	try {
		assert.equal(await entry.armAutoPlayNext({ outDeck: 1, inDeck: 2, fromMs: 1000 }), false);
		const plays = entry.getDispatchPerformanceCommandCalls().filter((cmd) => cmd.type === 'play' && cmd.deck === 2);
		assert.deepEqual(plays.map((cmd) => cmd.playing), [false, true]);
	} finally {
		planner.restore();
	}
});

test('a refused abort leaves the plan cancellable', async () => {
	_setupLoadedDecks(entry);
	const planner = _planner(entry, {});
	let refuse = true;
	const unregister = entry.registerAutoPlayNextPlanRunner({
		schedule: async () => ({ failed: null }),
		abort: async (id) => {
			if (refuse) throw new Error('abort refused');
			planner.calls.aborted.push(id);
		},
		status: () => planner.state.status
	});
	try {
		assert.equal(await entry.armAutoPlayNext(), true);
		await assert.rejects(entry.cancelAutoPlayNext(), /abort refused/);
		assert.equal(entry.autoPlayNextState.armed, true);
		refuse = false;
		await entry.cancelAutoPlayNext();
		assert.deepEqual(planner.calls.aborted, ['plan-1']);
		assert.equal(entry.autoPlayNextState.armed, false);
	} finally {
		unregister();
		planner.restore();
	}
});

// requirement: AGENT-49
test('only a completed plan is saved, and a replay sends no plan', async () => {
	_setupLoadedDecks(entry);
	let planner = _planner(entry, {});
	try {
		assert.equal(await entry.armAutoPlayNext(), true);
		planner.state.status = { id: 'plan-1', status: 'aborted' };
		await new Promise((resolve) => setTimeout(resolve, 300));
		assert.deepEqual(planner.calls.played, [], 'an aborted plan is not saved');
		planner.state.status = { id: 'plan-1', status: 'scheduled' };
		assert.equal(await entry.armAutoPlayNext(), true);
		planner.state.status = { id: 'plan-1', status: 'done' };
		await new Promise((resolve) => setTimeout(resolve, 300));
		assert.deepEqual(planner.calls.played, [
			{ out: 'outgoing-track', in: 'incoming-track', plan: { id: 'doc' }, source: 'planner', style: 'long_blend' }
		]);
	} finally {
		planner.restore();
	}
	planner = _planner(entry, { saved: true });
	try {
		assert.equal(await entry.armAutoPlayNext(), true);
		planner.state.status = { id: 'plan-1', status: 'done' };
		await new Promise((resolve) => setTimeout(resolve, 300));
		assert.deepEqual(planner.calls.played, [{ out: 'outgoing-track', in: 'incoming-track' }]);
	} finally {
		planner.restore();
	}
});
