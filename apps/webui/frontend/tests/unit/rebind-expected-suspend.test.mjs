/**
 * The output re-bind's own suspend is not an audio drop (issue #5887).
 *
 * The re-bind cycles a running context through suspend() and resume() to
 * re-open a stalled output stream. The context watchdog saw that `suspended`
 * edge while a deck played and raised "Audio stopped: audio output was
 * suspended", then raced the re-bind's resume with its own recovery loop (Tue
 * 15 Sep 2026 and Wed 7 Oct 2026 16:00:31Z on silver).
 *
 * [if] the re-bind suspends a playing context [then] no "Audio stopped" toast
 *   and no unexpected-pause row - broken otherwise (a false alarm mid-set)
 * [if] the re-bind's resume fails and leaves the context suspended [then] the
 *   watchdog still retries it once the cycle ends - broken otherwise (silence
 *   with nothing trying)
 * [if] a context is suspended with no re-bind in flight [then] the alarm and
 *   recovery run exactly as before (control: the excuse must not overshoot)
 * [if] a context is interrupted or closed during a re-bind [then] it still
 *   alarms: the re-bind never causes those
 * [if] the context was already suspended when the re-bind started (a genuine
 *   drop whose statechange is still queued) [then] that drop is still
 *   reported: only a suspend the re-bind itself issues is excused
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
before(async () => {
	mod = await loadTypeScriptModule('tests/unit/fixtures/rebind-watchdog-entry.ts');
});

/** A context with statechange events, suspend and resume. */
function fakeContext({ resumeFails = false } = {}) {
	const listeners = new Set();
	const ctx = {
		state: 'running',
		resumes: 0,
		addEventListener: (type, h) => type === 'statechange' && listeners.add(h),
		removeEventListener: (type, h) => type === 'statechange' && listeners.delete(h),
		async suspend() {
			ctx.__enter('suspended');
		},
		async resume() {
			ctx.resumes += 1;
			if (ctx.resumeFails) throw new Error('device gone');
			ctx.__enter('running');
		},
		resumeFails,
		__enter(state) {
			ctx.state = state;
			for (const h of listeners) h();
		}
	};
	return ctx;
}

function watchdogEffects() {
	const toasts = [];
	const pauses = [];
	return {
		toasts,
		pauses,
		effects: {
			pushToast: (message, kind = 'info') => toasts.push({ message, kind }),
			recordPerfTiming: () => {},
			sleep: async () => {
				await Promise.resolve();
			},
			noteUnexpectedPause: (state) => pauses.push(state)
		}
	};
}

function rebindEffects() {
	const toasts = [];
	const timers = [];
	return {
		toasts,
		timers,
		effects: {
			pushToast: (message, kind) => toasts.push({ message, kind }),
			recordPerfEvent: () => {},
			now: () => 1_000_000,
			setTimeout: (fn) => {
				timers.push(fn);
				return fn;
			},
			clearTimeout: () => {}
		}
	};
}

async function settle() {
	for (let i = 0; i < 300; i += 1) await Promise.resolve();
}

async function runRebind(ctx, wd) {
	const rb = rebindEffects();
	const uninstallRebind = mod.installOutputRebind(ctx, rb.effects, () => true, null);
	const detach = mod.installAudioContextWatchdog(ctx, wd.effects, () => true, 1_000);
	mod.noteOutputStall(1);
	for (const fn of rb.timers.splice(0)) fn();
	await settle();
	return () => {
		detach();
		if (typeof uninstallRebind === 'function') uninstallRebind();
		else uninstallRebind?.uninstall?.();
	};
}

test('the re-bind suspending a playing context raises no "Audio stopped" alarm', async () => {
	const ctx = fakeContext();
	const wd = watchdogEffects();
	const done = await runRebind(ctx, wd);
	assert.equal(ctx.state, 'running');
	assert.deepEqual(wd.toasts.filter((t) => /Audio stopped/.test(t.message)), [], 'its own suspend toasted as a drop');
	assert.deepEqual(wd.pauses, [], 'its own suspend was recorded as an unexpected pause');
	assert.equal(ctx.resumes, 1, 'the watchdog raced the re-bind with its own resume');
	done();
});

test('a re-bind whose resume fails leaves the context to the watchdog retries', async () => {
	const ctx = fakeContext({ resumeFails: true });
	const wd = watchdogEffects();
	const done = await runRebind(ctx, wd);
	assert.equal(ctx.state, 'suspended');
	assert.ok(ctx.resumes > 1, `only the re-bind's own resume ran (${ctx.resumes}); nothing retried after it`);
	done();
});

test('CONTROL: a suspend with no re-bind in flight still alarms and recovers', async () => {
	const ctx = fakeContext();
	const wd = watchdogEffects();
	const detach = mod.installAudioContextWatchdog(ctx, wd.effects, () => true, 1_000);
	ctx.__enter('suspended');
	await settle();
	assert.equal(wd.toasts.filter((t) => /Audio stopped/.test(t.message)).length, 1);
	assert.deepEqual(wd.pauses, ['suspended']);
	assert.equal(ctx.state, 'running');
	detach();
});

test('CONTROL: interrupted or closed during a re-bind still alarms', async () => {
	for (const state of ['interrupted', 'closed']) {
		const ctx = fakeContext();
		const wd = watchdogEffects();
		const detach = mod.installAudioContextWatchdog(ctx, wd.effects, () => true, 1_000);
		const release = mod.beginExpectedSuspend(ctx);
		ctx.__enter(state);
		await settle();
		release();
		assert.deepEqual(wd.pauses, [state], `${state} during a re-bind was excused`);
		detach();
	}
});

test('the release is idempotent and the excuse ends with it', async () => {
	const ctx = fakeContext();
	const wd = watchdogEffects();
	const detach = mod.installAudioContextWatchdog(ctx, wd.effects, () => true, 1_000);
	const release = mod.beginExpectedSuspend(ctx);
	release();
	release();
	ctx.__enter('suspended');
	await settle();
	assert.deepEqual(wd.pauses, ['suspended'], 'a released excuse still hid a real suspend');
	detach();
});

test('overlapping expected suspends keep the excuse until the last release', async () => {
	// The count is what the release checks (Sol P1 BLOCKING on #8045): one
	// release of two must leave the excuse up, and the second must take it down.
	const ctx = fakeContext();
	const wd = watchdogEffects();
	const detach = mod.installAudioContextWatchdog(ctx, wd.effects, () => true, 1_000);
	const first = mod.beginExpectedSuspend(ctx);
	const second = mod.beginExpectedSuspend(ctx);
	first();
	ctx.__enter('suspended');
	await settle();
	assert.deepEqual(wd.pauses, [], 'one release of two dropped the excuse');
	ctx.state = 'running';
	second();
	ctx.__enter('suspended');
	await settle();
	assert.deepEqual(wd.pauses, ['suspended'], 'the last release left the excuse up');
	detach();
});

test('pausing during a re-bind whose resume fails still leaves the context to the watchdog', async () => {
	// Codex P2 on #8045: the excuse swallowed the `suspended` edge while a deck
	// played, the operator paused while the resume was pending, the resume
	// failed, and the release's opportunity was then refused by the playing
	// gate. Nothing retried. The edge was accepted when it landed.
	const ctx = fakeContext({ resumeFails: true });
	let playing = true;
	let openGate;
	const gate = new Promise((resolve) => {
		openGate = resolve;
	});
	let first = true;
	ctx.resume = async () => {
		ctx.resumes += 1;
		if (first) {
			first = false;
			await gate;
		}
		throw new Error('device gone');
	};
	const wd = watchdogEffects();
	const rb = rebindEffects();
	const uninstallRebind = mod.installOutputRebind(ctx, rb.effects, () => playing, null);
	const detach = mod.installAudioContextWatchdog(ctx, wd.effects, () => playing, 1_000);
	mod.noteOutputStall(1);
	for (const fn of rb.timers.splice(0)) fn();
	await settle();
	playing = false;
	openGate();
	await settle();
	assert.equal(ctx.state, 'suspended');
	assert.ok(ctx.resumes > 1, `only the re-bind's own resume ran (${ctx.resumes}); the pause dropped recovery`);
	detach();
	if (typeof uninstallRebind === 'function') uninstallRebind();
	else uninstallRebind?.uninstall?.();
});

test('CONTROL: an excused suspend while nothing plays is not retried once paused', async () => {
	// The overshoot: carrying acceptance must not turn every expected suspend
	// into a retry. One that landed with no deck playing was never accepted.
	const ctx = fakeContext({ resumeFails: true });
	const wd = watchdogEffects();
	const detach = mod.installAudioContextWatchdog(ctx, wd.effects, () => false, 1_000);
	const release = mod.beginExpectedSuspend(ctx);
	ctx.__enter('suspended');
	await settle();
	release();
	await settle();
	assert.equal(ctx.resumes, 0, 'an edge that was never accepted was retried');
	detach();
});

test('a context already suspended when the re-bind starts is still reported as a drop', async () => {
	// The browser queued the genuine `suspended` statechange behind a busy main
	// thread; the re-bind's debounce fires first, sees `suspended`, skips its
	// own suspend and goes straight to resume. The queued event lands while the
	// resume is in flight. Nothing the re-bind did caused it, so it must alarm.
	const ctx = fakeContext();
	let openGate;
	const gate = new Promise((resolve) => {
		openGate = resolve;
	});
	ctx.resume = async () => {
		ctx.resumes += 1;
		await gate;
		ctx.__enter('running');
	};
	const wd = watchdogEffects();
	const rb = rebindEffects();
	const uninstallRebind = mod.installOutputRebind(ctx, rb.effects, () => true, null);
	const detach = mod.installAudioContextWatchdog(ctx, wd.effects, () => true, 1_000);
	ctx.state = 'suspended';
	mod.noteOutputStall(1);
	for (const fn of rb.timers.splice(0)) fn();
	await settle();
	ctx.__enter('suspended');
	await settle();
	openGate();
	await settle();
	assert.deepEqual(wd.pauses, ['suspended'], 'a drop the re-bind did not cause was excused as its own');
	detach();
	if (typeof uninstallRebind === 'function') uninstallRebind();
	else uninstallRebind?.uninstall?.();
});
