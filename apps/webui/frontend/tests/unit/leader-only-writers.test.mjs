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
const { installLeaderOnlyEventWriters, installLeaderOnlyRestore } = await loadTypeScriptModule(
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

test('re-promotion restarts the writers but never restores decks again', async () => {
	const leadership = fakeLeadership(true);
	const r = recorder();
	installBoth(leadership, r.deps);
	await flush();
	leadership.set(false);
	leadership.set(true);
	await flush();
	assert.equal(r.log.filter((line) => line === 'rescue restore').length, 1);
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
		const at = onMount.indexOf(call);
		assert.ok(at >= 0, `${call} is still wired`);
		const gate = Math.max(
			onMount.lastIndexOf('installLeaderOnlyRestore({', at),
			onMount.lastIndexOf('installLeaderOnlyEventWriters({', at)
		);
		assert.ok(gate >= 0 && onMount.slice(gate, at).split('});').length === 1, `${call} runs outside the leader gate`);
	}
	assert.match(page, /installUiMirror\(tabLeadership\.leadership\)/);
	assert.match(page, /tabLeadership\.dispose\(\);/);
});
