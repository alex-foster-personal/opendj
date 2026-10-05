/**
 * AGENT-18 follow-up: a follower /performance tab is a pure viewer.
 *
 * Mon 5 Oct 2026: a second tab read the browser's shared session snapshot,
 * loaded all four of the playing tab's decks (URL gained d1..d4), fetched their
 * audio, and then its session writer overwrote the snapshot. Session restore,
 * rescue restore and the rescue ring writer, the play counter and the set
 * recorder's deck observer now run only in the leader tab.
 *
 * Regression lines:
 *   - if a follower restores decks or installs any writer then broken
 *   - if promotion does not install the writers then broken
 *   - if losing leadership leaves a writer running then broken
 *   - if a re-promoted tab restores its decks a second time then broken
 *   - if leadership lost mid rescue fetch still installs the session writer then broken
 *   - if the route's own unmount leaves a writer running then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { whileLeader } = await loadTypeScriptModule('src/lib/rb/tab-leadership.ts');
const { installDemotionSilencer, installLeaderOnlyEventWriters, installLeaderOnlyRestore, silenceDemotedTab } = await loadTypeScriptModule(
	'src/lib/rb/leader-only-writers.ts'
);

const flush = async () => {
	for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
};

/** A leadership whose role the test drives, with the controller's subscribe contract. */
function fakeLeadership(leads) {
	let leader = leads;
	const listeners = new Set();
	return {
		isLeader: () => leader,
		subscribe(listener) {
			listeners.add(listener);
			return () => listeners.delete(listener);
		},
		set(next) {
			leader = next;
			for (const listener of [...listeners]) listener({ role: next ? 'leader' : 'follower' });
		},
		listenerCount: () => listeners.size
	};
}

/** Real-shaped installers that record what ran and what is still live. */
function recorder({ rescueHandled = false, rescueDelay = null } = {}) {
	const log = [];
	const live = new Set();
	const installer = (name) => () => {
		log.push(`install ${name}`);
		live.add(name);
		return () => {
			log.push(`uninstall ${name}`);
			live.delete(name);
		};
	};
	return {
		log,
		live,
		deps: {
			runRescueAutoRestore: async () => {
				log.push('rescue restore');
				if (rescueDelay !== null) await rescueDelay;
				return rescueHandled;
			},
			installSessionRestore: ({ skipDeckRestore }) => {
				log.push(`session restore skipDeckRestore=${skipDeckRestore}`);
				return installer('session writer')();
			},
			installRescueRingWriter: installer('rescue writer'),
			installDeckObserver: installer('deck observer'),
			installPlayCounter: installer('play counter')
		}
	};
}

function installBoth(leadership, deps) {
	const stopEvents = installLeaderOnlyEventWriters({ leadership, ...deps });
	const stopRestore = installLeaderOnlyRestore({ leadership, ...deps });
	return () => {
		stopEvents();
		stopRestore();
	};
}

test('a follower restores nothing and installs no writer', async () => {
	const leadership = fakeLeadership(false);
	const r = recorder();
	installBoth(leadership, r.deps);
	await flush();
	assert.deepEqual(r.log, []);
	assert.equal(r.live.size, 0);
});

test('mutation control: the same installers without the gate run in a follower', async () => {
	// What the route did before AGENT-18: install unconditionally.
	const r = recorder();
	r.deps.installDeckObserver();
	r.deps.installPlayCounter();
	await r.deps.runRescueAutoRestore();
	r.deps.installSessionRestore({ skipDeckRestore: false });
	assert.ok(r.log.includes('session restore skipDeckRestore=false'));
	assert.equal(r.live.size, 3);
});

test('the leader restores decks once and runs every writer', async () => {
	const leadership = fakeLeadership(true);
	const r = recorder();
	installBoth(leadership, r.deps);
	await flush();
	assert.deepEqual(r.log, [
		'install deck observer',
		'install play counter',
		'rescue restore',
		'session restore skipDeckRestore=false',
		'install session writer',
		'install rescue writer'
	]);
});

test('a rescue-handled restore still skips the session deck restore', async () => {
	const leadership = fakeLeadership(true);
	const r = recorder({ rescueHandled: true });
	installBoth(leadership, r.deps);
	await flush();
	assert.ok(r.log.includes('session restore skipDeckRestore=true'));
});

test('a follower promoted later restores then, and losing leadership stops every writer', async () => {
	const leadership = fakeLeadership(false);
	const r = recorder();
	installBoth(leadership, r.deps);
	await flush();
	leadership.set(true);
	await flush();
	assert.deepEqual(
		[...r.live].sort(),
		['deck observer', 'play counter', 'rescue writer', 'session writer']
	);
	leadership.set(false);
	assert.equal(r.live.size, 0, `still live after demotion: ${[...r.live]}`);
});

test('re-promotion resumes from the rescue ring but never re-applies the session decks', async () => {
	const leadership = fakeLeadership(true);
	const r = recorder();
	installBoth(leadership, r.deps);
	await flush();
	leadership.set(false);
	leadership.set(true);
	await flush();
	assert.equal(r.log.filter((line) => line === 'rescue restore').length, 2, 'Take control continues the set from the rescue ring');
	assert.deepEqual(
		r.log.filter((line) => line.startsWith('session restore')),
		['session restore skipDeckRestore=false', 'session restore skipDeckRestore=true']
	);
	assert.equal(r.live.size, 4);
});

test('leadership lost during the rescue fetch installs no session writer', async () => {
	let release;
	const r = recorder({ rescueDelay: new Promise((resolve) => (release = resolve)) });
	const leadership = fakeLeadership(true);
	installBoth(leadership, r.deps);
	await flush();
	leadership.set(false);
	release();
	await flush();
	assert.equal(r.log.some((line) => line.startsWith('session restore')), false);
	assert.equal(r.live.size, 0);
});

test('the route unmount stops every writer and unsubscribes', async () => {
	const leadership = fakeLeadership(true);
	const r = recorder();
	const stop = installBoth(leadership, r.deps);
	await flush();
	stop();
	assert.equal(r.live.size, 0);
	assert.equal(leadership.listenerCount(), 0);
	leadership.set(false);
	leadership.set(true);
	await flush();
	assert.equal(r.live.size, 0, 'a stale subscription must not reinstall after unmount');
});

test('whileLeader passes how many times it ran before', () => {
	const leadership = fakeLeadership(true);
	const runs = [];
	whileLeader(leadership, (prior) => {
		runs.push(prior);
		return () => {};
	});
	leadership.set(false);
	leadership.set(true);
	leadership.set(true);
	assert.deepEqual(runs, [0, 1], 'a repeated leader notification must not reinstall');
});

test('/performance routes every writer through the leader gate', async () => {
	const { readFileSync } = await import('node:fs');
	const page = readFileSync(new URL('../../src/routes/performance/+page.svelte', import.meta.url), 'utf8');
	const onMount = page.slice(page.indexOf('onMount(() => {'));
	for (const call of [
		'installPerformanceSessionRestore(',
		'installRescueRingWriter(',
		'runPerformanceRescueAutoRestore(',
		'installPlayCounter(',
		'installDeckObserverEmitter('
	]) {
		const sites = [...onMount.matchAll(new RegExp(call.replace('(', '\\('), 'g'))].map((m) => m.index);
		assert.ok(sites.length > 0, `${call} is still wired`);
		for (const at of sites) {
			const gate = Math.max(
				onMount.lastIndexOf('installLeaderOnlyRestore({', at),
				onMount.lastIndexOf('installLeaderOnlyEventWriters({', at)
			);
			const between = onMount.slice(gate, at);
			assert.ok(gate >= 0 && !between.includes('});'), `${call} runs outside the leader gate`);
		}
	}
	assert.match(page, /installUiMirror\(tabLeadership\.leadership\)/);
	assert.match(page, /tabLeadership\.dispose\(\);/);
});

// ------------------------------------------------- bug #31: go silent ---

/** A fake audio graph: per deck a main source and stem sources, each running or not. */
function fakeGraph(playingDecks) {
	const nodes = [];
	for (const deck of [1, 2, 3, 4]) {
		const playing = playingDecks.includes(deck);
		nodes.push({ deck, kind: 'deck', running: playing });
		for (const stem of ['vocals', 'drums']) nodes.push({ deck, kind: `stem-${stem}`, running: playing });
	}
	nodes.push({ deck: null, kind: 'preview-cue', running: true });
	let autoplayArmed = true;
	return {
		running: () => nodes.filter((node) => node.running).map((node) => `${node.kind}@${node.deck}`),
		autoplayArmed: () => autoplayArmed,
		effects: {
			// engine.pause stops a deck and every stem riding it.
			pauseDeck: async (deck) => {
				for (const node of nodes) if (node.deck === deck) node.running = false;
			},
			stopPreviewCue: () => {
				for (const node of nodes) if (node.kind === 'preview-cue') node.running = false;
			},
			cancelAutoPlayNext: () => {
				autoplayArmed = false;
			},
			reportError: (deck, error) => {
				throw new Error(`deck ${deck}: ${error}`);
			}
		}
	};
}

test('bug #31: after demotion no audio node in the demoted tab is running', async () => {
	const leadership = fakeLeadership(true);
	const graph = fakeGraph([3, 1]);
	installDemotionSilencer({ leadership, silence: () => silenceDemotedTab(graph.effects) });
	assert.ok(graph.running().length > 0, 'precondition: the leader is playing');
	leadership.set(false);
	await flush();
	assert.deepEqual(graph.running(), []);
	assert.equal(graph.autoplayArmed(), false, 'AutoPlay cannot start a deck behind the new leader');
});

test('mutation control: demotion without the silencer leaves the set playing', async () => {
	const leadership = fakeLeadership(true);
	const graph = fakeGraph([3]);
	leadership.set(false);
	await flush();
	assert.ok(graph.running().includes('deck@3'));
	assert.ok(graph.running().includes('stem-vocals@3'));
});

test('mutation control: a silence that pauses only playing-flagged decks misses an armed one', async () => {
	// A quantized launch armed on a paused deck reads playing=false; pausing every
	// deck is what clears it. Pausing only decks 1..3 leaves deck 4 running here.
	const graph = fakeGraph([4]);
	const partial = { ...graph.effects, pauseDeck: async (deck) => (deck === 4 ? undefined : graph.effects.pauseDeck(deck)) };
	silenceDemotedTab(partial);
	await flush();
	assert.deepEqual(graph.running(), ['deck@4', 'stem-vocals@4', 'stem-drums@4']);
});

test('the silencer runs after every leader-only writer has stopped', async () => {
	const leadership = fakeLeadership(true);
	const r = recorder();
	const order = [];
	// Installed FIRST, as /performance does, so its listener runs before the gates'.
	installDemotionSilencer({ leadership, silence: () => order.push('silence') });
	installBoth(leadership, {
		...r.deps,
		installRescueRingWriter: () => () => order.push('rescue writer stopped'),
		installPlayCounter: () => () => order.push('play counter stopped')
	});
	await flush();
	leadership.set(false);
	await flush();
	assert.equal(order.at(-1), 'silence', JSON.stringify(order));
	assert.ok(order.includes('rescue writer stopped') && order.includes('play counter stopped'));
});

test('the silencer fires only on losing leadership, never at install or as a follower', async () => {
	const leadership = fakeLeadership(false);
	let silences = 0;
	const dispose = installDemotionSilencer({ leadership, silence: () => (silences += 1) });
	leadership.set(false);
	await flush();
	assert.equal(silences, 0, 'a follower that stays a follower is not silenced again');
	leadership.set(true);
	await flush();
	assert.equal(silences, 0, 'promotion is not demotion');
	leadership.set(false);
	await flush();
	assert.equal(silences, 1);
	leadership.set(true);
	leadership.set(false);
	dispose();
	await flush();
	assert.equal(silences, 1, 'after unmount the route teardown owns stopping, not the silencer');
});

test('a demotion that is reversed in the same tick does not silence', async () => {
	const leadership = fakeLeadership(true);
	let silences = 0;
	installDemotionSilencer({ leadership, silence: () => (silences += 1) });
	leadership.set(false);
	leadership.set(true);
	await flush();
	assert.equal(silences, 0);
});
