import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/** [if] rebuildOnce fails then recovers on a later attempt [then] decks stay loaded and reattaching clears. */
test('graph-rebuild-retry controller recovers after a failed rebuildOnce', async () => {
	const { createGraphRebuildRetryController } = await loadTypeScriptModule(
		'src/lib/rb/deck-channel-graph.ts'
	);
	const harness = makePorts({ retryDelaysMs: [1], retrySleep: async () => {} });
	let calls = 0;
	harness.ports.recreateGraphKeepingDecks = async () => {
		calls += 1;
		harness.attempts.push(calls);
		if (calls === 1) {
			harness.ports.runtime(1).processor = null;
			harness.ports.runtime(2).processor = null;
			throw new Error('addModule timed out');
		}
		harness.ports.runtime(1).processor = {};
		harness.ports.runtime(2).processor = {};
	};
	const controller = createGraphRebuildRetryController(harness.ports);
	await controller.rebuildAudioGraphKeepingDecks();
	assert.deepEqual(harness.attempts, [1, 2]);
	assert.deepEqual(controller.reattachingDecks(), []);
	assert.equal(harness.ports.state(1).stable_id, 'aaaa');
});

/** [if] retries are exhausted [then] orphaned decks get GRAPH_REBUILD_EXHAUSTED unload. */
test('graph-rebuild-retry controller unloads when every retry fails', async () => {
	const { createGraphRebuildRetryController, GRAPH_REBUILD_EXHAUSTED_MESSAGE } =
		await loadTypeScriptModule('src/lib/rb/deck-channel-graph.ts');
	const harness = makePorts({ retryDelaysMs: [1, 2], retrySleep: async () => {} });
	harness.ports.recreateGraphKeepingDecks = async () => {
		harness.ports.runtime(1).processor = null;
		harness.ports.runtime(2).processor = null;
		throw new Error('addModule timed out');
	};
	const controller = createGraphRebuildRetryController(harness.ports);
	await assert.rejects(() => controller.rebuildAudioGraphKeepingDecks(), /addModule timed out/);
	assert.equal(harness.unloads.length, 2);
	assert.match(harness.unloads[0], new RegExp(GRAPH_REBUILD_EXHAUSTED_MESSAGE));
	assert.equal(harness.ports.state(1).stable_id, null);
	assert.equal(harness.ports.state(2).stable_id, null);
});

/** [if] AutoPlay is on and a deck was playing [then] play runs after recovery. */
test('graph-rebuild-retry controller resumes playing decks when AutoPlay is on', async () => {
	const { createGraphRebuildRetryController } = await loadTypeScriptModule(
		'src/lib/rb/deck-channel-graph.ts'
	);
	const harness = makePorts({
		autoPlay: true,
		deck1Playing: true,
		retryDelaysMs: [1],
		retrySleep: async () => {}
	});
	let calls = 0;
	harness.ports.recreateGraphKeepingDecks = async () => {
		calls += 1;
		if (calls === 1) {
			harness.ports.runtime(1).processor = null;
			throw new Error('stall');
		}
		harness.ports.runtime(1).processor = {};
	};
	const controller = createGraphRebuildRetryController(harness.ports);
	await controller.rebuildAudioGraphKeepingDecks();
	assert.deepEqual(harness.played, [1]);
	assert.equal(harness.ports.state(1).processor_error, null);
});

/** [if] AutoPlay is off and a deck was playing [then] decks get press-play prompt. */
test('graph-rebuild-retry controller prompts press play when AutoPlay is off', async () => {
	const { createGraphRebuildRetryController, ENGINE_RECOVERED_PRESS_PLAY } =
		await loadTypeScriptModule('src/lib/rb/deck-channel-graph.ts');
	const harness = makePorts({
		autoPlay: false,
		deck1Playing: true,
		retryDelaysMs: [1],
		retrySleep: async () => {}
	});
	let calls = 0;
	harness.ports.recreateGraphKeepingDecks = async () => {
		calls += 1;
		if (calls === 1) {
			harness.ports.runtime(1).processor = null;
			throw new Error('stall');
		}
		harness.ports.runtime(1).processor = {};
	};
	const controller = createGraphRebuildRetryController(harness.ports);
	await controller.rebuildAudioGraphKeepingDecks();
	assert.deepEqual(harness.played, []);
	assert.equal(harness.ports.state(1).processor_error, ENGINE_RECOVERED_PRESS_PLAY);
});

/** [if] a retry is scheduled [then] reattaching decks are marked until recovery. */
test('graph-rebuild-retry controller marks reattaching between attempts', async () => {
	const { createGraphRebuildRetryController } = await loadTypeScriptModule(
		'src/lib/rb/deck-channel-graph.ts'
	);
	let seenDuringRetry = null;
	let calls = 0;
	/** @type {ReturnType<typeof createGraphRebuildRetryController> | undefined} */
	let controller;
	const harness = makePorts({
		deck1Playing: true,
		retryDelaysMs: [1],
		retrySleep: async () => {
			seenDuringRetry = {
				reattaching: controller.reattachingDecks(),
				deck1: { ...harness.ports.state(1) }
			};
		}
	});
	harness.ports.recreateGraphKeepingDecks = async () => {
		calls += 1;
		if (calls === 1) {
			harness.ports.runtime(1).processor = null;
			harness.ports.runtime(2).processor = null;
			throw new Error('stall');
		}
		harness.ports.runtime(1).processor = {};
		harness.ports.runtime(2).processor = {};
	};
	controller = createGraphRebuildRetryController(harness.ports);
	await controller.rebuildAudioGraphKeepingDecks();
	assert.deepEqual(seenDuringRetry.reattaching, [1, 2]);
	assert.match(seenDuringRetry.deck1.processor_error ?? '', /re-?attaching/);
	assert.equal(harness.markRecoveryReason, 'graph-rebuild-failed');
});

function makePorts({ autoPlay = false, deck1Playing = false, retryDelaysMs, retrySleep } = {}) {
	const deckIds = [1, 2];
	const runtimeByDeck = new Map(
		deckIds.map((deck) => [
			deck,
			{
				audioBuffer: {},
				processor: {},
				desiredActive: deck === 1 && deck1Playing,
				controlActive: deck === 1 && deck1Playing,
				pending: [],
				presentation: null,
				nextScheduleRevision: 0,
				metadataDurationMs: 180_000
			}
		])
	);
	const stateByDeck = new Map(
		deckIds.map((deck) => [
			deck,
			{
				deck_id: deck,
				position_ms: 60_000,
				stable_id: deck === 1 ? 'aaaa' : 'bbbb',
				playing: deck === 1 && deck1Playing,
				audible: deck === 1 && deck1Playing,
				transport_pending: false,
				processor_error: null,
				duration_ms: 180_000
			}
		])
	);
	const attempts = [];
	const stretchTimeouts = [];
	const unloads = [];
	const played = [];
	let markRecoveryReason = null;
	const ports = {
		deckIds,
		runtime(deck) {
			return runtimeByDeck.get(deck);
		},
		state(deck) {
			return stateByDeck.get(deck);
		},
		contextState: () => 'running',
		autoPlayEnabled: () => autoPlay,
		setStretchAddModuleTimeoutMs(ms) {
			stretchTimeouts.push(ms);
		},
		resetStretchAddModuleTimeoutMs() {},
		async recreateGraphKeepingDecks() {
			throw new Error('recreate not wired in this test');
		},
		async resumeContext() {},
		createPresentedTransportTimeline: (sec) => ({ sec }),
		recordUnexpectedPause() {},
		withPauseOrigin(_origin, fn) {
			return fn();
		},
		recordDeckStopOf() {},
		recordPerfEvent() {},
		recordProcessorFailure(deck, error) {
			unloads.push(error.message);
			const st = stateByDeck.get(deck);
			st.stable_id = null;
			runtimeByDeck.get(deck).audioBuffer = null;
			runtimeByDeck.get(deck).processor = null;
		},
		markEngineRecoveryStop(reason) {
			markRecoveryReason = reason;
		},
		async play(deck) {
			played.push(deck);
		},
		retryDelaysMs,
		retrySleep
	};
	return {
		ports,
		attempts,
		stretchTimeouts,
		unloads,
		played,
		get markRecoveryReason() {
			return markRecoveryReason;
		}
	};
}
