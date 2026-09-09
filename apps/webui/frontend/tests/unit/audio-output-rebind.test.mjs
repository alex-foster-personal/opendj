/**
 * Output re-bind after the device changed under a running context.
 *
 * Wed 2 Sep 2026 18:01 CEST: a phone call took the Bluetooth headphones for a
 * second and Open DJ stayed silent afterwards. The context never left
 * `running` (Chromium), so the statechange watchdog had nothing to see; the
 * presentation clock reported a stall and nothing acted on it.
 *
 * [if] a devicechange arrives while a deck plays [then] exactly one
 *   suspend+resume cycle runs - broken otherwise (silence stays)
 * [if] two devicechange events land inside the debounce [then] still one cycle
 * [if] the presentation clock stalls [then] the same cycle runs
 * [if] nothing is playing [then] no cycle (a startup-suspended context is not an alarm)
 * [if] a second trigger inside the cooldown [then] no SECOND cycle yet (a
 *   flapping device must not loop the graph) but the request is HELD and runs
 *   when the cooldown expires - dropping it leaves the graph on the device that
 *   went away, which is the Wed 9 Sep 2026 17:44:43Z cutout
 * [if] a device flaps A -> B -> A inside the cooldown [then] the graph ends up
 *   re-bound for the LAST edge, not stranded on the middle one - broken otherwise
 * [if] ten changes land inside one cooldown [then] still exactly one extra
 *   cycle, coalesced (the rate limit the cooldown exists for is intact)
 * [if] a deferred request is pending when the route unmounts [then] uninstall
 *   cancels it rather than cycling a closed context
 * [if] nothing is playing when a request lands inside the cooldown [then] it is
 *   dropped, not held: there is no audio to save 10s from now
 * [if] a trigger lands while a suspend/resume is still IN FLIGHT [then] it is
 *   held and runs once that cycle completes - returning early on `rebinding`
 *   drops the settling edge exactly like the cooldown used to
 * [if] the operator pauses after a request was accepted but before the cooldown
 *   expires [then] the held request still runs - re-gating it on playback
 *   strands the graph on the vanished device, and resuming cannot repair it
 *   because _resumeContext() only resumes a SUSPENDED context
 * [if] the operator pauses during the 400ms DEBOUNCE, after a device change
 *   arrived while a deck was playing [then] the cycle still runs: playback is
 *   sampled at the trigger, not after the timer
 * [if] a rebind throws [then] the perf row is recorded at ERROR severity so it
 *   escalates to the engine log - at info it never leaves the browser
 * [if] the context is not running [then] resume only, no suspend
 * [if] the stall reporter's edge does not call noteOutputStall [then] the
 *   trigger is wired to nothing - broken
 * [if] a rebind uninstalled at route unmount still answers a stall [then] it
 *   resumes a closed context and the user sees a spurious error toast - broken
 * [if] disarmContextInstrumentation does not uninstall the rebind [then] every
 *   /performance remount leaves another listener armed - broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function harness({ playing = true, state = 'running', resumeThrows = false, gateResume = false } = {}) {
	const calls = [];
	const perf = [];
	// A rebind is two awaits, and the device that is going away is the one whose
	// suspend/resume is slow - so the cycle outliving the 400ms debounce is the
	// normal case, not an exotic one. `gateResume` parks the cycle inside
	// `resume()` until the test releases it, which is the only way to exercise
	// the in-flight branch at all: an ungated harness resolves both awaits on
	// microtasks and `rebinding` is never observably true.
	let releaseResume = () => {};
	const resumeGate = gateResume ? new Promise((r) => { releaseResume = r; }) : null;
	const ctx = {
		state,
		async suspend() { calls.push('suspend'); this.state = 'suspended'; },
		async resume() {
			calls.push('resume');
			if (resumeGate !== null) await resumeGate;
			if (resumeThrows) throw new Error('device gone');
			this.state = 'running';
		}
	};
	let now = 0;
	const timers = [];
	const effects = {
		pushToast: (m, k) => calls.push(`toast:${k}`),
		recordPerfEvent: (kind, message, severity) => {
			calls.push(`perf:${kind}`);
			perf.push({ kind, message, severity });
		},
		now: () => now,
		setTimeout: (fn, ms) => { const h = { fn, at: now + ms }; timers.push(h); return h; },
		clearTimeout: (h) => { const i = timers.indexOf(h); if (i >= 0) timers.splice(i, 1); }
	};
	const listeners = [];
	const mediaDevices = {
		addEventListener: (_t, h) => listeners.push(h),
		removeEventListener: (_t, h) => { const i = listeners.indexOf(h); if (i >= 0) listeners.splice(i, 1); }
	};
	async function advance(ms) {
		now += ms;
		// Keep the not-yet-due timers. `splice(0)` used to remove EVERY timer and
		// only re-run the due ones, so a cycle armed for later was silently
		// deleted by the next advance - a harness that can only ever prove
		// "nothing fired".
		const due = timers.filter((t) => t.at <= now);
		for (const t of due) timers.splice(timers.indexOf(t), 1);
		for (const t of due) t.fn();
		await new Promise((r) => setImmediate(r));
		await new Promise((r) => setImmediate(r));
	}
	return { ctx, effects, calls, perf, mediaDevices, fireDeviceChange: () => listeners.forEach((h) => h()), advance, isPlaying: () => playing, releaseResume: () => releaseResume() };
}

describe('installOutputRebind', () => {
	let mod;
	before(async () => {
		mod = await loadTypeScriptModule('src/lib/rb/audio-output-rebind.ts');
	});

	it('one devicechange while playing = one suspend+resume cycle, toasted and logged', async () => {
		const h = harness();
		mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.deepEqual(h.calls, ['suspend', 'resume', 'perf:audio-output-rebound', 'toast:info'],
			'if a device change while playing does not cycle the context then the output stays dead - broken');
	});

	it('two devicechange events inside the debounce = still one cycle', async () => {
		const h = harness();
		mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		h.fireDeviceChange();
		await h.advance(100);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.equal(h.calls.filter((c) => c === 'suspend').length, 1, 'if the debounce does not coalesce then a device flap double-cycles - broken');
	});

	it('a presentation-clock stall triggers the same cycle', async () => {
		const h = harness();
		mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		mod.noteOutputStall(2);
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.deepEqual(h.calls.slice(0, 2), ['suspend', 'resume'], 'if a stalled output position does not re-bind then the phone-call case stays silent - broken');
	});

	it('nothing playing = no cycle', async () => {
		const h = harness({ playing: false });
		mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.deepEqual(h.calls, [], 'if an idle context is cycled on every device change then startup becomes noisy - broken');
	});

	it('a second trigger inside the cooldown does not cycle again YET', async () => {
		const h = harness();
		mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.equal(h.calls.filter((c) => c === 'suspend').length, 1,
			'if the cooldown does not hold then a flapping device loops the graph - broken');
		assert.ok(h.calls.includes('perf:audio-output-rebind-deferred'),
			'if a suppressed request leaves no row then the cutout is invisible in the log - broken');
		await h.advance(mod.REBIND_COOLDOWN_MS);
		assert.equal(h.calls.filter((c) => c === 'suspend').length, 2,
			'if the held request never runs then the graph stays on the device that went away - broken');
	});

	// THE 17:44:43Z CUTOUT, AS A TEST. On the Air the system default output
	// flipped BuiltInSpeakerDevice -> Bluetooth and back 1.9s later. Before this
	// fix the second edge hit the cooldown and was discarded outright, leaving
	// the graph bound to headphones that had already gone: silent until restart.
	it('a device that flaps and settles ends up re-bound for the LAST edge', async () => {
		const h = harness();
		mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		h.fireDeviceChange();                        // built-in -> bluetooth
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.equal(h.calls.filter((c) => c === 'resume').length, 1);
		h.fireDeviceChange();                        // bluetooth -> built-in, 1.9s later
		await h.advance(1_500);
		// Nothing further is asked of the app; only the clock moves, exactly as on
		// the Air. The cycle has to arrive on its own.
		await h.advance(mod.REBIND_COOLDOWN_MS);
		assert.equal(h.calls.filter((c) => c === 'resume').length, 2,
			'if the settled device never gets a cycle then Open DJ stays silent until restart - broken');
	});

	it('ten changes inside one cooldown coalesce to exactly one extra cycle', async () => {
		const h = harness();
		mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		for (let i = 0; i < 10; i++) {
			h.fireDeviceChange();
			await h.advance(mod.REBIND_DEBOUNCE_MS);
		}
		assert.equal(h.calls.filter((c) => c === 'suspend').length, 1,
			'if a flap loops the graph then the rate limit this cooldown exists for is gone - broken');
		await h.advance(mod.REBIND_COOLDOWN_MS);
		assert.equal(h.calls.filter((c) => c === 'suspend').length, 2,
			'if ten held requests each fire then the fix traded a dropped cycle for a storm - broken');
	});

	it('nothing playing = a cooldown-era request is dropped, not held', async () => {
		const h = harness({ playing: false });
		mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		await h.advance(mod.REBIND_COOLDOWN_MS * 2);
		assert.deepEqual(h.calls, [],
			'if an idle device change is held then a cycle fires 10s after the operator stopped - broken');
	});

	// SOL P1 (discussion on line 93): `if (rebinding) return` sat ABOVE the
	// cooldown handling, so a request landing during an in-flight cycle was
	// discarded outright - the same defect the cooldown branch was just fixed
	// for, one state earlier and with no perf row either.
	it('a trigger landing while a rebind is IN FLIGHT is held, not dropped', async () => {
		const h = harness({ gateResume: true });
		mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		h.fireDeviceChange();                          // built-in -> bluetooth
		await h.advance(mod.REBIND_DEBOUNCE_MS);       // cycle starts, parks in resume()
		assert.equal(h.calls.filter((c) => c === 'resume').length, 1,
			'the harness must actually be parked mid-cycle, or this test proves nothing');
		h.fireDeviceChange();                          // bluetooth -> built-in, still in flight
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.ok(h.calls.includes('perf:audio-output-rebind-deferred'),
			'if a request landing mid-cycle leaves no row then it was dropped silently - broken');
		h.releaseResume();
		await h.advance(0);                            // cycle completes and re-arms the hold
		await h.advance(mod.REBIND_COOLDOWN_MS);
		assert.equal(h.calls.filter((c) => c === 'resume').length, 2,
			'if a request that lands during an in-flight rebind is discarded then the graph stays on the device that went away - broken');
	});

	// CODEX P1 (discussion_r3972521486): the debounce is the FIRST delay between
	// a trigger and its cycle, and the playing gate was sampled after it - so a
	// pause inside the 400ms window discarded a trigger that was live when the
	// device actually changed. Same defect class as the two below, one state
	// earlier.
	it('a trigger accepted while playing survives a pause during the DEBOUNCE', async () => {
		let playing = true;
		const h = harness();
		mod.installOutputRebind(h.ctx, h.effects, () => playing, h.mediaDevices);
		h.fireDeviceChange();                          // arrives while a deck plays
		await h.advance(200);                          // still inside the debounce
		playing = false;                               // operator pauses
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.equal(h.calls.filter((c) => c === 'suspend').length, 1,
			'if playback is sampled after the debounce then pausing within 400ms of a device change strands the graph on the vanished output - broken');
	});

	// SOL P1 (discussion on line 98): the playing gate ran again when the held
	// request fired, so pausing anywhere inside the 10s cooldown threw it away.
	it('a request held by the cooldown survives the operator pausing', async () => {
		let playing = true;
		const h = harness();
		mod.installOutputRebind(h.ctx, h.effects, () => playing, h.mediaDevices);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);       // cycle 1 runs
		h.fireDeviceChange();                          // lands inside the cooldown, HELD
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.ok(h.calls.includes('perf:audio-output-rebind-deferred'),
			'the request must actually be held, or the pause below tests nothing');
		playing = false;                               // operator pauses mid-cooldown
		await h.advance(mod.REBIND_COOLDOWN_MS);
		assert.equal(h.calls.filter((c) => c === 'suspend').length, 2,
			'if pausing during the cooldown discards an accepted request then the graph stays on the vanished device, and resuming cannot repair it because _resumeContext() only resumes a SUSPENDED context - broken');
	});

	it('uninstall cancels a request still held by the cooldown', async () => {
		const h = harness();
		const handle = mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		handle.uninstall();
		await h.advance(mod.REBIND_COOLDOWN_MS);
		assert.equal(h.calls.filter((c) => c === 'suspend').length, 1,
			'if a held cycle survives uninstall then a closed context is suspended and the user sees a spurious error toast - broken');
	});

	it('a failed rebind is recorded at ERROR severity so it leaves the browser', async () => {
		const h = harness({ resumeThrows: true });
		mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		const failed = h.perf.find((row) => row.kind === 'audio-output-rebind-failed');
		assert.ok(failed, 'a rejected resume must record a failure row');
		assert.equal(failed.severity, 'error',
			'if the failure is recorded below error then recordPerfEvent never escalates it and a dead output leaves no server-side trace - broken');
	});

	it('a non-running context is resumed, not suspended first', async () => {
		const h = harness({ state: 'suspended' });
		mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.deepEqual(h.calls.slice(0, 1), ['resume']);
	});

	it('uninstall removes the device listener and the stall hook', async () => {
		const h = harness();
		const handle = mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		handle.uninstall();
		h.fireDeviceChange();
		mod.noteOutputStall(1);
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.deepEqual(h.calls, []);
	});

	it('an uninstalled rebind stops answering stalls', async () => {
		// The route-remount path: /performance unmounts (engine.dispose closes the
		// context), then remounts and builds a new one. `noteOutputStall` fans out
		// to EVERY registered listener, so one left behind answers the next stall
		// by resuming a CLOSED context -- which rejects, and puts an "output could
		// not be re-bound" error toast next to the live context's success toast.
		const gone = harness();
		const goneHandle = mod.installOutputRebind(gone.ctx, gone.effects, gone.isPlaying, gone.mediaDevices);
		goneHandle.uninstall();

		const live = harness();
		const liveHandle = mod.installOutputRebind(live.ctx, live.effects, live.isPlaying, live.mediaDevices);
		try {
			mod.noteOutputStall(1);
			// BOTH clocks advance. Advancing only the live one would pass whether or
			// not the stale listener is still registered, since its timer lives on
			// its own harness -- the assertion below would then prove nothing.
			await gone.advance(mod.REBIND_DEBOUNCE_MS);
			await live.advance(mod.REBIND_DEBOUNCE_MS);

			assert.deepEqual(gone.calls, [],
				'if an uninstalled rebind still answers a stall then a closed context is resumed and the user sees a spurious error toast - broken');
			assert.ok(live.calls.includes('resume'),
				'if the live context does not rebind then the fix broke the thing it is fixing - broken');
		} finally {
			liveHandle.uninstall();
		}
	});
});

describe('wiring (source guards; the engine and reporter are rune-bound)', () => {
	const report = readFileSync(fileURLToPath(new URL('../../src/lib/rb/presentation-clock-report.ts', import.meta.url)), 'utf8');
	const instr = readFileSync(fileURLToPath(new URL('../../src/lib/rb/audio-context-instrumentation.ts', import.meta.url)), 'utf8');

	// `armAudioContextWatchdog` also calls `_outputRebind?.uninstall()` (it
	// clears the previous handle before arming a fresh one), so a loose
	// `disarmContextInstrumentation[\s\S]*?...` regex still matches there even
	// if the teardown's own call is deleted. Delimit the body by brace-counting
	// from the function's opening `{` so the checks below can only match inside
	// `disarmContextInstrumentation` itself.
	const disarmStart = instr.indexOf('export function disarmContextInstrumentation(');
	assert.ok(disarmStart !== -1, 'disarmContextInstrumentation not found in source');
	const braceOpen = instr.indexOf('{', disarmStart);
	let depth = 0, i = braceOpen;
	for (; i < instr.length; i++) {
		if (instr[i] === '{') depth++;
		else if (instr[i] === '}' && --depth === 0) break;
	}
	const disarmBody = instr.slice(braceOpen + 1, i);

	it('the stall edge calls noteOutputStall', () => {
		assert.ok(report.includes('noteOutputStall(deck)'), 'if the stall reporter does not call noteOutputStall then the phone-call trigger is wired to nothing - broken');
	});

	it('the rebind is installed next to the context watchdog', () => {
		assert.ok(instr.includes('installOutputRebind('), 'if armAudioContextWatchdog does not install the rebind then no device change ever re-binds - broken');
	});

	it('disarmContextInstrumentation uninstalls the rebind', () => {
		assert.ok(
			disarmBody.includes('_outputRebind?.uninstall()'),
			'if teardown does not uninstall the rebind then every /performance remount leaves another listener armed on a closed context - broken'
		);
	});

	it('disarmContextInstrumentation drops the agent-facing output reader', () => {
		assert.ok(
			/delete \(window[\s\S]*?\)\.__mdtAudioOutput/.test(disarmBody),
			'if teardown leaves __mdtAudioOutput installed then agents read a frozen verdict for a disposed context - broken'
		);
	});
});
