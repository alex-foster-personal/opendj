/**
 * ADVERSARIAL (round 2, S1): a pitch fader on a synced master.
 *
 * A MIDI pitch fader sends a message per step (action-glue.svelte.ts
 * `deck_pitch` -> `void dispatchPerformanceCommand({ type: 'tempo', ... })`)
 * and the on-screen fader does the same per input event (Deck.svelte
 * `setTempo`). Every tempo command was queued FIFO on [deck, 'sync']
 * (performance-ipc.svelte.ts `performanceCommandQueueScopes`). On a master
 * with playing followers each one re-locks the group at a shared instant
 * (`_synchronizeFollowers`) and the next waits for that instant to pass
 * (`pendingSyncWaitTarget`, schedule-math.ts) while holding the scopes, so N
 * fader messages cost N group schedules back to back. What a DJ hears: the
 * tempo keeps moving for seconds after the fader has stopped. Measured in the
 * real engine by performance-beat-sync-adversarial.spec.ts (S1): 25.4 s.
 *
 * Fix (performance-ipc.svelte.ts `_latestTempoTicket`): every queued tempo
 * takes a ticket, and a queued tempo that a newer tempo for the same deck has
 * overtaken runs as a no-op. These tests drive the REAL ScopedCommandScheduler
 * with the dispatcher's OWN ticket statements, extracted from its source and
 * evaluated here, so a mutation of the dispatcher reaches them.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const DISPATCHER = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url)),
	'utf8'
);
// Loose on purpose: whatever the dispatcher's ticket statements say is what runs
// below, so a mutation there changes behavior here instead of a regex miss.
const MARK = DISPATCHER.match(/const tempoTicket = [^\n]*\n\t[^\n]*_latestTempoTicket\.set\([^\n]*/);
const RUNS = DISPATCHER.match(/\n\t\t\tif \((.*)\) \{\s*await _execute\(command, pressT0Ms\);/);

let ScopedCommandScheduler;

before(async () => {
	({ ScopedCommandScheduler } = await loadTypeScriptModule(
		'src/lib/rb/performance-command-scheduler.ts'
	));
});

/** The dispatcher's ticket state and its two statements, as written there. */
function dispatcherTickets() {
	assert.ok(MARK, 'the dispatcher no longer marks queued tempos as pinned here');
	assert.ok(RUNS, 'the dispatcher no longer gates _execute on the tempo ticket as pinned here');
	const state = { _latestTempoTicket: new Map(), _tempoTickets: 0 };
	const mark = new Function(
		'state',
		'command',
		'deck',
		`const { _latestTempoTicket } = state; let _tempoTickets = state._tempoTickets;
		${MARK[0]}
		state._tempoTickets = _tempoTickets; return tempoTicket;`
	);
	const runs = new Function(
		'state',
		'deck',
		'tempoTicket',
		`const { _latestTempoTicket } = state; return ${RUNS[1]};`
	);
	return {
		mark: (command, deck) => mark(state, command, deck),
		runs: (deck, ticket) => runs(state, deck, ticket)
	};
}

function deferred() {
	let resolve;
	const promise = new Promise((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

/** The dispatcher's wiring, reduced to its moving parts. Each executed command
 * holds its scopes until released, like a tempo on a synced master that waits
 * for the previous group schedule. `executed` lists what reached the engine. */
function harness() {
	const scheduler = new ScopedCommandScheduler();
	const tickets = dispatcherTickets();
	const executed = [];
	const gates = [];
	const submit = (command, deck, scopes, label) => {
		const tempoTicket = tickets.mark(command, deck);
		return scheduler.run(scopes, async () => {
			if (!tickets.runs(deck, tempoTicket)) return 'skipped';
			executed.push(label);
			if (command.type !== 'tempo') return label;
			const gate = deferred();
			gates.push(gate);
			await gate.promise;
			return command.ratio;
		});
	};
	const tempo = (deck, ratio) =>
		submit({ type: 'tempo', deck, ratio }, deck, [deck, 'sync'], `tempo${deck}=${ratio}`);
	const other = (label, deck, scopes) => submit({ type: label }, deck, scopes, label);
	// A real fader message is its own event, so the previous one has already
	// started by the time the next arrives. Model that turn explicitly.
	const tick = () => new Promise((resolve) => setImmediate(resolve));
	const drain = async () => {
		for (let i = 0; i < 200; i++) {
			await new Promise((resolve) => setImmediate(resolve));
			for (const gate of gates.splice(0)) gate.resolve();
		}
	};
	return { tempo, other, executed, drain, tick };
}

test('S1: 30 fader messages on a busy master reach the engine twice, ending on the last value', async () => {
	const { tempo, executed, drain, tick } = harness();
	const ratios = Array.from({ length: 30 }, (_, i) => Number((1 + (i + 1) * 0.001).toFixed(3)));
	const results = [];
	for (const ratio of ratios) {
		results.push(tempo(1, ratio));
		await tick();
	}
	await drain();
	const settled = await Promise.all(results);
	// Without the tracker this is 30 executions: 30 serialized group schedules.
	assert.deepEqual(executed, ['tempo1=1.001', 'tempo1=1.03']);
	assert.equal(settled[0], 1.001);
	assert.equal(settled.at(-1), 1.03);
	assert.equal(settled.filter((value) => value === 'skipped').length, 28);
});

test('S1 control: the last tempo always executes, even with nothing to supersede it', async () => {
	const { tempo, executed, drain } = harness();
	const only = tempo(1, 1.05);
	await drain();
	assert.equal(await only, 1.05);
	assert.deepEqual(executed, ['tempo1=1.05']);
});

test('S1 control: nothing is reordered - a command between two tempos runs before the later one', async () => {
	const { tempo, other, executed, drain, tick } = harness();
	const first = tempo(1, 1.01); // starts at once, holds the scopes
	await tick();
	const queued = tempo(1, 1.02); // overtaken below, becomes a no-op
	const play = other('play', 1, [1, 'sync']);
	const later = tempo(1, 1.03);
	await drain();
	await Promise.all([first, queued, play, later]);
	assert.deepEqual(executed, ['tempo1=1.01', 'play', 'tempo1=1.03']);
});

test('S1: two faders moving at once each drop their own overtaken steps and end on their own last value', async () => {
	const { tempo, executed, drain, tick } = harness();
	const results = [];
	for (let i = 1; i <= 20; i++) {
		results.push(tempo(1, 1 + i / 1000));
		results.push(tempo(2, 1 - i / 1000));
		await tick();
	}
	await drain();
	await Promise.all(results);
	const deckOne = executed.filter((r) => r.startsWith('tempo1'));
	const deckTwo = executed.filter((r) => r.startsWith('tempo2'));
	assert.ok(deckOne.length <= 2 && deckTwo.length <= 2, JSON.stringify(executed));
	assert.equal(deckOne.at(-1), 'tempo1=1.02');
	assert.equal(deckTwo.at(-1), 'tempo2=0.98');
});

test('S1 control: a tempo already executing is never cut short by a newer one', async () => {
	const { tempo, executed, drain, tick } = harness();
	const first = tempo(1, 1.01);
	await tick();
	assert.deepEqual(executed, ['tempo1=1.01'], 'the first tempo started at once');
	const second = tempo(1, 1.02);
	await drain();
	assert.equal(await first, 1.01, 'the running tempo completed with its own value');
	assert.equal(await second, 1.02);
	assert.deepEqual(executed, ['tempo1=1.01', 'tempo1=1.02']);
});

test('S1 control: a non-tempo command on the same deck is never skipped', async () => {
	const { tempo, other, executed, drain, tick } = harness();
	const first = tempo(1, 1.01);
	await tick();
	const eq = other('eq', 1, [1]);
	const later = tempo(1, 1.02);
	await drain();
	await Promise.all([first, eq, later]);
	assert.deepEqual(executed, ['tempo1=1.01', 'eq', 'tempo1=1.02']);
});

test('S1 source pin: the ticket is taken on the queued path, after the unqueued branches', () => {
	const mark = DISPATCHER.indexOf('? ++_tempoTickets : 0;');
	assert.ok(mark > DISPATCHER.indexOf('if (scopes === null) {'), 'mark after the unqueued branch');
	assert.ok(mark < DISPATCHER.indexOf('_commandScheduler.run(scopes, run)'), 'mark before queuing');
});
