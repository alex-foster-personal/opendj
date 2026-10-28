/**
 * AGENT-30: the bus side of a scheduled step set.
 *
 * [if] at_context_sec is more than MAX_SCHEDULE_AHEAD_S ahead [then ⛔] it is refused (Sol P2 #7594).
 * [if] a step is scheduled [then ⛔] its mirror write (and the level handoff that reads it) waits for it to land, and a
 *   plain set on that control cancels the waiting write (Sol P1 #7594).
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const { DeferredMirrorWrites, MAX_SCHEDULE_AHEAD_S, ScheduledSteps, scheduledStartSec } = await loadTypeScriptModule('src/lib/rb/scheduled-mixer-write.ts');

test('a scheduled time is bounded to a short window ahead; a past one starts now', () => {
	assert.equal(scheduledStartSec(undefined, 10), 10);
	assert.equal(scheduledStartSec(10.05, 10), 10.05);
	assert.equal(scheduledStartSec(9, 10), 10);
	assert.throws(() => scheduledStartSec(10 + MAX_SCHEDULE_AHEAD_S + 0.01, 10), /at most/);
	assert.throws(() => scheduledStartSec(3600, 10), /at most/);
	assert.throws(() => scheduledStartSec(Number.NaN, 10), /not a time/);
});

function harness() {
	const timers = [];
	const clock = { now: 0 };
	const writes = new DeferredMirrorWrites(
		(callback, ms) => {
			const handle = { callback, ms, cleared: false };
			timers.push(handle);
			return handle;
		},
		(handle) => (handle.cleared = true),
		() => clock.now
	);
	// Fire every live timer once at audio time `now` (the default: far past every step).
	const fire = (now = 1e9) => {
		clock.now = now;
		timers.filter((t) => !t.cleared).forEach((t) => ((t.cleared = true), t.callback()));
	};
	return { writes, timers, fire, clock };
}

test('a scheduled mirror write waits for its step to land', () => {
	const { writes, fire } = harness();
	let mirror = 1;
	writes.write('fader:1', 0.05, () => (mirror = 0));
	assert.equal(mirror, 1, 'the mirror still shows what the room hears');
	fire();
	assert.equal(mirror, 0);
});

test('a plain set cancels a waiting write on its control; another control is untouched (control)', () => {
	const { writes, fire } = harness();
	let fader = 1, eq = 0.5;
	writes.write('fader:1', 0.05, () => (fader = 0));
	writes.write('eq:1:low', 0.05, () => (eq = 0));
	writes.write('fader:1', null, () => (fader = 0.8));
	assert.equal(fader, 0.8);
	fire();
	assert.equal(fader, 0.8, 'the cancelled write never lands');
	assert.equal(eq, 0);
});

test('a timer that wakes before the audio clock reaches the step waits again (suspended context)', () => {
	// Codex BLOCKING on #7594: a wall-clock timer moved the mirror while the context was suspended.
	const { writes, fire } = harness();
	let fader = 1;
	writes.write('fader:1', 0.05, () => (fader = 0));
	fire(0.01); // the timer fired but the context has only reached 10 ms
	assert.equal(fader, 1, 'not landed on the audio clock yet');
	fire(0.05);
	assert.equal(fader, 0, 'lands once the audio clock reaches it');
});

test('two scheduled writes on one control both land, in time order, as both automations do', () => {
	// Sol P1 on #7594: the second replaced the first's mirror write while the first's automation still ran.
	const { writes, fire } = harness();
	const seen = [];
	writes.write('fader:1', 0.05, () => seen.push(0.5));
	writes.write('fader:1', 0.08, () => seen.push(0));
	fire(0.06);
	assert.deepEqual(seen, [0.5], 'the first lands at its own time');
	fire(0.08);
	assert.deepEqual(seen, [0.5, 0]);
});

test('the engine routes scheduled fader and EQ steps through ScheduledSteps and keeps the plain sets as they were', async () => {
	// CI triage on #7594: the plain setFader/setEq signatures are LATENCY-03's pinned wiring, so a scheduled step has its
	// own entry point (scheduleStep) and the IPC picks it only when `at_context_sec` is set.
	const source = await readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	assert.ok(source.includes('setFader(deck: DeckId, value: number, pressT0Ms?: number): void {'));
	assert.ok(source.includes('setEq(deck: DeckId, band: EqBand, value: number, pressT0Ms?: number): void {'));
	const step = source.slice(source.indexOf('scheduleStep(deck: DeckId'), source.indexOf('scheduleStep(deck: DeckId') + 1400);
	assert.match(step, /_steps\.schedule\(stepId, key, atContextSec, startAudio, \(\) => \{ ch\[[^\]]+\] = value; _maybeHandoffOnAir\(\); \}\)/);
	const fader = source.slice(source.indexOf('setFader(deck: DeckId'), source.indexOf('setFader(deck: DeckId') + 500);
	assert.match(fader, /_steps\.cancelPlain\(`fader:\$\{deck\}`/, 'a plain fader set cancels a waiting step');
	const eq = source.slice(source.indexOf('setEq(deck: DeckId'), source.indexOf('setEq(deck: DeckId') + 400);
	assert.match(eq, /_steps\.cancelPlain\(`eq:\$\{deck\}:\$\{band\}`/, 'a plain eq set cancels a waiting step');
	const ipc = await readFrontendSource('src/lib/rb/performance-ipc.svelte.ts');
	assert.ok(ipc.includes("if (command.at_context_sec !== undefined) engine.scheduleStep(command.deck, 'fader', command.value, command.at_context_sec, command.step_id!);"));
	assert.ok(ipc.includes('else engine.setFader(command.deck, command.value, pressT0Ms);'));
	assert.ok(ipc.includes('if (command.at_context_sec !== undefined) engine.scheduleStep(command.deck, command.band, command.value, command.at_context_sec, command.step_id!);'));
	assert.ok(ipc.includes('else engine.setEq(command.deck, command.band, command.value, pressT0Ms);'));
});

test('ScheduledSteps: a plain set cancels the automation only when a step was waiting on that control', () => {
	const calls = [];
	const param = { cancelScheduledValues: (t) => calls.push(t) };
	let now = 10;
	const timers = [];
	const steps = new ScheduledSteps(() => now, (cb, ms) => (timers.push(cb), cb), () => {});
	steps.cancelPlain('fader:1', param);
	assert.deepEqual(calls, [], 'control: nothing waiting, the plain path never touches the param');
	let started = null;
	steps.schedule('s1', 'fader:1', 10.05, (start) => (started = start), () => {});
	assert.equal(started, 10.05);
	steps.cancelPlain('fader:1', param);
	assert.deepEqual(calls, [10], 'a waiting step is cancelled at the current context time');
	assert.throws(() => new ScheduledSteps(() => null).schedule('s2', 'fader:1', 1, () => {}, () => {}), /not initialised/);
});

test('engine disposal drops every waiting mirror write, so none lands on the next session', async () => {
	// Codex BLOCKING on #7594: a directly dispatched scheduled fader outlived dispose() and moved the new mirror.
	const { writes, fire } = harness();
	let fader = 1;
	writes.write('fader:1', 0.05, () => (fader = 0));
	writes.cancelAll();
	fire();
	assert.equal(fader, 1, 'the cancelled write never lands');
	writes.write('fader:1', 0.05, () => (fader = 0)); // control: a write made after the cancel still lands
	fire();
	assert.equal(fader, 0);
	const source = await readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const dispose = source.slice(source.indexOf('async dispose(): Promise<void> {'), source.indexOf('async dispose(): Promise<void> {') + 300);
	assert.match(dispose, /_engineSession \+= 1;\n\t+_steps\.cancelAll\(\);/);
});

test('ScheduledSteps refuses a step earlier than one still waiting on its control, so audio and mirror never diverge', () => {
	// Codex BLOCKING #7594: a kill at 10.10 then a boost at 10.05 left the audio boosted and the mirror showing the kill.
	let now = 10;
	const steps = new ScheduledSteps(() => now, () => 'timer', () => {});
	const started = [];
	steps.schedule('s3', 'eq:1:low', 10.1, (at) => started.push(at), () => {});
	assert.throws(() => steps.schedule('s4', 'eq:1:low', 10.05, (at) => started.push(at), () => {}), /earlier than one still waiting/);
	assert.deepEqual(started, [10.1], 'the refused step never touched the audio');
	steps.schedule('s5', 'eq:1:low', 10.2, (at) => started.push(at), () => {}); // control: a later step on it is fine
	steps.schedule('s6', 'eq:1:mid', 10.05, (at) => started.push(at), () => {}); // control: another control is independent
	assert.deepEqual(started, [10.1, 10.2, 10.05]);
});


test('two writes on one control land in audio-time order, whatever order their wake-ups run in', () => {
	// Sol P1 #7594: independent retry timers could wake out of order after a suspend and leave the older value.
	const timers = [];
	const clock = { now: 0 };
	const writes = new DeferredMirrorWrites((cb) => (timers.push(cb), cb), () => {}, () => clock.now);
	const seen = [];
	writes.write('fader:1', 0.05, () => seen.push(0.5));
	writes.write('fader:1', 0.08, () => seen.push(0));
	clock.now = 1; // resumed long after both
	for (const wake of [...timers].reverse()) wake(); // every wake-up ever armed, newest first
	assert.deepEqual(seen, [0.5, 0], 'the earlier write first, the later one last, once each');
});

test('ScheduledSteps: a step is waiting until its mirror write applies on the audio clock, then landed', () => {
	// Sol P1 #7594: explicit state, owned here. Landed needs the audio time AND the write applied.
	const timers = [];
	const clock = { now: 10 };
	const steps = new ScheduledSteps(() => clock.now, (cb) => (timers.push(cb), cb), () => {});
	let mirror = 1;
	steps.schedule('plan:3:1', 'fader:1', 10.05, () => {}, () => (mirror = 0));
	assert.equal(steps.state('plan:3:1'), 'waiting');
	clock.now = 10.06; // the audio time has passed but no wake-up has run: the write has not applied
	assert.equal(steps.state('plan:3:1'), 'waiting', 'not landed on the clock alone');
	timers.at(-1)();
	assert.equal(mirror, 0);
	assert.equal(steps.state('plan:3:1'), 'landed');
	steps.schedule('plan:4:2', 'fader:1', 10.2, () => {}, () => {});
	steps.cancelPlain('fader:1', null);
	assert.equal(steps.state('plan:4:2'), 'cancelled', 'a plain set cancels at once');
	steps.schedule('plan:5:3', 'eq:1:low', 10.3, () => {}, () => {});
	steps.cancelAll();
	assert.equal(steps.state('plan:5:3'), 'lost', 'engine teardown: a waiting step is lost, not cancelled by anyone');
	assert.equal(steps.state('plan:4:2'), 'cancelled', 'and a finished state is kept');
	assert.equal(steps.state('never'), null);
});

test('ScheduledSteps: after engine teardown a step id may be scheduled again; a waiting id may not', () => {
	// Codex + Sol #7594: a remounted page could not reuse an id; uniqueness only binds a step that still waits.
	const steps = new ScheduledSteps(() => 10, () => 'timer', () => {});
	steps.schedule('plan:r1:3:1', 'fader:1', 10.05, () => {}, () => {});
	assert.throws(() => steps.schedule('plan:r1:3:1', 'eq:1:low', 10.1, () => {}, () => {}), /already scheduled and waiting/, 'control');
	steps.cancelAll();
	assert.equal(steps.state('plan:r1:3:1'), 'lost');
	steps.schedule('plan:r1:3:1', 'fader:1', 10.05, () => {}, () => {});
	assert.equal(steps.state('plan:r1:3:1'), 'waiting');
});

test('the engine cancels pending steps whenever a graph context is retired, not only on dispose', async () => {
	// Codex BLOCKING #7594: a graph rebuild replaced the context but kept the old steps, read against the new clock.
	const source = await readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const reset = source.slice(source.indexOf('function _resetGraphState('), source.indexOf('function _resetGraphState(') + 700);
	assert.match(reset, /_ctx = null; _steps\.cancelAll\(\);/);
});

test('ScheduledSteps keeps a finished state until its runner forgets it, however many other steps finish', () => {
	// Sol P1 #7594: a 256-entry cap evicted a landed state before its runner read it; the null replayed it as lost.
	const timers = [];
	const clock = { now: 10 };
	const steps = new ScheduledSteps(() => clock.now, (cb) => (timers.push(cb), cb), () => {});
	steps.schedule('first', 'fader:1', 10.01, () => {}, () => {});
	clock.now = 10.02;
	timers.at(-1)();
	for (let i = 0; i < 600; i += 1) {
		steps.schedule(`other:${i}`, `fader:${2 + (i % 3)}`, clock.now + 0.001, () => {}, () => {});
		clock.now += 0.002;
		timers.at(-1)();
	}
	assert.equal(steps.state('first'), 'landed', 'still landed after 600 other steps finished');
	steps.forget('first');
	assert.equal(steps.state('first'), null, 'dropped once its runner forgot it');
	steps.schedule('pending', 'eq:1:low', clock.now + 1, () => {}, () => {});
	steps.forget('pending');
	assert.equal(steps.state('pending'), 'waiting', 'control: a waiting step is never forgotten');
});

test('the engine refuses a scheduled step with no target AudioParam, by name, before anything is queued', async () => {
	// Sol P1 #7594: with no channel nodes the step was queued and later read landed although no audio moved.
	const { readFrontendSource } = await import('./engine-source.mjs');
	const source = await readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const body = source.slice(source.indexOf('\tscheduleStep(deck: DeckId'), source.indexOf('\tsetCrossfader('));
	const refuse = body.indexOf('if (param === null) throw new Error(`no_audio_param:');
	assert.ok(refuse > 0, 'refused with a named reason');
	assert.ok(refuse < body.indexOf('_steps.schedule('), 'before the step is queued');
	assert.doesNotMatch(body, /param === null \? undefined/, 'no silent no-op left for a missing param');
});
