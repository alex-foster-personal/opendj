/**
 * A context rendering into a dead output device must become an ERROR state.
 *
 * Wed 2 Sep 2026 18:33 CEST: deck playing, context running, HAL clock advancing,
 * mixer open, silence watchdog happy, no sound. A fresh context in the same tab
 * had a 192 ms output latency; the app's had 0 and nobody looked at it again.
 *
 * [if] a running context with a playing deck reports outputLatency 0 for two
 *   polls [then] an error toast, an error perf row and a re-bind request - broken
 *   otherwise (silence stays silent)
 * [if] it stays 0 for four polls [then] the sticky reload escalation
 * [if] latency comes back [then] "restored" info once, verdict ok
 * [if] teardown does not uninstall the poll [then] a route remount leaves
 *   another interval waking forever - broken
 * [if] nothing is playing, or the context is not running [then] never alarms
 *   (a fresh context is 0 for a moment after resume; grace is the whole point)
 * [if] latency is > 0 throughout [then] verdict ok, no toast
 * [if] armAudioContextWatchdog does not install the liveness poll [then] the
 *   detector is code nobody runs - broken
 * [if] onSnapshot is provided [then] it fires on every poll (idle included)
 *   with the current verdict, not only on a toast-worthy transition - the
 *   output-health bar (pin 93c82bb36eb7) has no other way to reflect "idle"
 *   or a still-dead poll that is not the alarm/escalation edge
 * [if] a running context with a playing deck reports the same getOutputTimestamp.contextTime
 *   for more than 2 s [then] verdict stalled, one error toast, one error perf row,
 *   one recoverOutput call
 * [if] outputLatency is > 0 but the timestamp is frozen [then] stalled, never ok
 * [if] the timestamp advances [then] verdict ok and recoverOutput is not called
 * [if] bug #58: the page is hidden, timers are throttled to 60 s and the output timestamp
 *   is frozen but currentTime (the render clock) advances [then] no stall, no recovery
 * [if] the page is hidden and the render clock is frozen too [then] stalled + recover once
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function harness({ playing = true, state = 'running', latency = 0, frozenTimestamp = false } = {}) {
	const calls = [];
	const snapshots = [];
	let nowMs = 0;
	let contextTime = 0.1;
	const ctx = {
		state,
		outputLatency: latency,
		baseLatency: 0.0058,
		sinkId: '',
		getOutputTimestamp: () => ({ contextTime, performanceTime: nowMs })
	};
	let intervalFn = null;
	const effects = {
		pushToast: (m, k) => calls.push(`toast:${k}:${m.slice(0, 20)}`),
		recordPerfEvent: (kind, _m, sev) => calls.push(`perf:${kind}:${sev}`),
		setInterval: (fn) => {
			intervalFn = fn;
			return 'h';
		},
		clearInterval: () => {
			intervalFn = null;
		},
		onSnapshot: (s) => snapshots.push(s),
		now: () => nowMs,
		recoverOutput: () => calls.push('recover')
	};
	return {
		ctx,
		effects,
		calls,
		snapshots,
		poll: () => intervalFn && intervalFn(),
		isPlaying: () => playing,
		advance(ms) {
			nowMs += ms;
			if (!frozenTimestamp) contextTime += ms / 1000;
		}
	};
}

describe('installOutputLiveness', () => {
	let mod;
	before(async () => {
		mod = await loadTypeScriptModule('src/lib/rb/audio-output-liveness.ts');
	});

	it('zero latency while playing for two polls = error toast + perf row + re-bind request', () => {
		const h = harness();
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		h.poll();
		assert.deepEqual(h.calls, [], 'one zero poll is grace, not an alarm');
		h.advance(mod.LIVENESS_POLL_MS);
		h.poll();
		assert.equal(live.verdict(), 'dead');
		assert.deepEqual(h.calls, ['perf:audio-output-dead:error', 'toast:error:NO AUDIO OUTPUT: the'],
			'if a dead output is not an error state then silence stays silent - broken');
		const src = readFileSync(fileURLToPath(new URL('../../src/lib/rb/audio-output-liveness.ts', import.meta.url)), 'utf8');
		const deadBranch = src.slice(src.indexOf("verdict = 'dead';"), src.indexOf("} else if (deadPolls === LIVENESS_ESCALATE_POLLS)"));
		assert.ok(deadBranch.includes('noteOutputStall(0)'), 'if the dead verdict does not request a re-bind then nothing tries to recover - broken');
	});

	it('four zero polls = sticky reload escalation, once', () => {
		const h = harness();
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		for (let i = 0; i < 5; i++) {
			h.advance(mod.LIVENESS_POLL_MS);
			h.poll();
		}
		assert.equal(live.verdict(), 'dead-escalated');
		assert.equal(h.calls.filter((c) => c.startsWith('perf:audio-output-dead-persistent')).length, 1);
		assert.equal(h.calls.filter((c) => c.startsWith('toast:error')).length, 2, 'exactly one alarm and one escalation, not a toast per poll');
	});

	it('latency returning flips to ok with one restored toast', () => {
		const h = harness();
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		h.advance(mod.LIVENESS_POLL_MS);
		h.poll();
		h.advance(mod.LIVENESS_POLL_MS);
		h.poll();
		h.ctx.outputLatency = 0.19;
		h.advance(100);
		h.poll();
		h.advance(100);
		h.poll();
		assert.equal(live.verdict(), 'ok');
		assert.equal(h.calls.filter((c) => c === 'toast:info:Audio output restore').length, 1);
	});

	it('nothing playing, or a non-running context, never alarms', () => {
		const idle = harness({ playing: false });
		mod.installOutputLiveness(idle.ctx, idle.effects, idle.isPlaying);
		for (let i = 0; i < 6; i++) {
			idle.advance(mod.LIVENESS_POLL_MS);
			idle.poll();
		}
		assert.deepEqual(idle.calls, []);
		const suspended = harness({ state: 'suspended' });
		mod.installOutputLiveness(suspended.ctx, suspended.effects, suspended.isPlaying);
		for (let i = 0; i < 6; i++) {
			suspended.advance(mod.LIVENESS_POLL_MS);
			suspended.poll();
		}
		assert.deepEqual(suspended.calls, []);
	});

	it('healthy latency throughout = ok and silent', () => {
		const h = harness({ latency: 0.19 });
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		for (let i = 0; i < 6; i++) {
			h.advance(100);
			h.poll();
		}
		assert.equal(live.verdict(), 'ok');
		assert.deepEqual(h.calls, []);
	});

	it('frozen timestamp with latency > 0 for > 2 s => stalled + recover once', () => {
		const h = harness({ latency: 0.19, frozenTimestamp: true });
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		h.poll();
		h.advance(mod.OUTPUT_STALL_MS + 1);
		h.poll();
		assert.equal(live.verdict(), 'stalled');
		assert.equal(h.calls.filter((c) => c === 'perf:audio-output-stalled:error').length, 1);
		assert.equal(h.calls.filter((c) => c.startsWith('toast:error:NO AUDIO OUTPUT:')).length, 1);
		assert.equal(h.calls.filter((c) => c === 'recover').length, 1);
		h.advance(100);
		h.poll();
		assert.equal(h.calls.filter((c) => c === 'recover').length, 1, 'still stalled must not re-recover');
	});

	it('bug #58: hidden page, throttled timers, healthy render clock => no stall, no recovery', () => {
		const h = harness({ latency: 0.19, frozenTimestamp: true });
		let renderTime = 1;
		Object.defineProperty(h.ctx, 'currentTime', { get: () => renderTime });
		h.effects.isHidden = () => true;
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		for (let i = 0; i < 5; i++) {
			h.advance(60_000);
			renderTime += 60;
			h.poll();
		}
		assert.equal(h.calls.filter((c) => c === 'recover').length, 0,
			'if a throttled hidden tab with a running render clock recovers then a healthy set is torn down - broken');
		assert.notEqual(live.verdict(), 'stalled');
	});

	it('bug #58 control: hidden page with the render clock frozen too still stalls and recovers once', () => {
		const h = harness({ latency: 0.19, frozenTimestamp: true });
		Object.defineProperty(h.ctx, 'currentTime', { get: () => 1 });
		h.effects.isHidden = () => true;
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		h.poll();
		h.advance(60_000);
		h.poll();
		assert.equal(live.verdict(), 'stalled', 'if a hidden tab can never stall then a real dead graph stays silent - broken');
		assert.equal(h.calls.filter((c) => c === 'recover').length, 1);
	});

	it('bug #58 control: a visible page keeps the #2155 output-timestamp verdict even if currentTime advances', () => {
		const h = harness({ latency: 0.19, frozenTimestamp: true });
		let renderTime = 1;
		Object.defineProperty(h.ctx, 'currentTime', { get: () => renderTime });
		h.effects.isHidden = () => false;
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		h.poll();
		h.advance(mod.OUTPUT_STALL_MS + 1);
		renderTime += 3;
		h.poll();
		assert.equal(live.verdict(), 'stalled');
	});

	it('advancing timestamp stays ok and never calls recoverOutput', () => {
		const h = harness({ latency: 0.19 });
		mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		for (let i = 0; i < 4; i++) {
			h.advance(800);
			h.poll();
		}
		assert.deepEqual(h.calls, []);
	});

	it('snapshot carries the fields an agent needs', () => {
		const h = harness({ latency: 0.192 });
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		h.advance(100);
		h.poll();
		const s = live.snapshot();
		assert.deepEqual(Object.keys(s).sort(), ['base_latency_ms', 'output_context_time_s', 'output_latency_ms', 'sink_id', 'state', 'verdict']);
		assert.equal(s.output_latency_ms, 192);
	});
});

describe('onSnapshot (pin 93c82bb36eb7: the output-health bar)', () => {
	let mod;
	before(async () => {
		mod = await loadTypeScriptModule('src/lib/rb/audio-output-liveness.ts');
	});

	it('fires on every poll, not only on a verdict change', () => {
		const h = harness({ latency: 0.19 });
		mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		h.advance(100);
		h.poll();
		h.advance(100);
		h.poll();
		h.advance(100);
		h.poll();
		assert.equal(h.snapshots.length, 3, 'a poll that produces no toast still owes the bar a reading');
		assert.deepEqual(h.snapshots.map((s) => s.verdict), ['ok', 'ok', 'ok']);
	});

	it('fires "idle" when nothing is playing, so the bar can go dark rather than stay stuck on a stale reading', () => {
		const h = harness({ playing: false });
		mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		h.poll();
		assert.equal(h.snapshots.length, 1);
		assert.equal(h.snapshots[0].verdict, 'idle');
	});

	it('carries the dead verdict on the poll that raises it, and dead-escalated on the fourth', () => {
		const h = harness();
		mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		h.advance(mod.LIVENESS_POLL_MS);
		h.poll();
		h.advance(mod.LIVENESS_POLL_MS);
		h.poll();
		assert.equal(h.snapshots.at(-1).verdict, 'dead');
		h.advance(mod.LIVENESS_POLL_MS);
		h.poll();
		h.advance(mod.LIVENESS_POLL_MS);
		h.poll();
		assert.equal(h.snapshots.at(-1).verdict, 'dead-escalated');
	});

	it('is optional: omitting it from effects does not throw', () => {
		const h = harness({ latency: 0.19 });
		delete h.effects.onSnapshot;
		mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		assert.doesNotThrow(() => h.poll());
	});
});

describe('wiring (source guard)', () => {
	it('armAudioContextWatchdog installs the liveness poll and exposes __mdtAudioOutput', () => {
		const src = readFileSync(fileURLToPath(new URL('../../src/lib/rb/audio-context-instrumentation.ts', import.meta.url)), 'utf8');
		assert.ok(src.includes('installOutputLiveness('), 'if the liveness poll is not installed at graph build then the detector is code nobody runs - broken');
		assert.ok(src.includes('__mdtAudioOutput'), 'if the snapshot is not exposed then an agent cannot read output health - broken');
		assert.ok(src.includes('isHidden: () =>'), 'if the master liveness is not told the page is hidden then a throttled tab false-recovers (bug #58) - broken');
	});

	it('disarmContextInstrumentation uninstalls the liveness poll', () => {
		const src = readFileSync(fileURLToPath(new URL('../../src/lib/rb/audio-context-instrumentation.ts', import.meta.url)), 'utf8');
		const disarmStart = src.indexOf('export function disarmContextInstrumentation(');
		assert.ok(disarmStart !== -1, 'disarmContextInstrumentation not found in source');
		const braceOpen = src.indexOf('{', disarmStart);
		let depth = 0, i = braceOpen;
		for (; i < src.length; i++) {
			if (src[i] === '{') depth++;
			else if (src[i] === '}' && --depth === 0) break;
		}
		const disarmBody = src.slice(braceOpen + 1, i);
		assert.ok(
			disarmBody.includes('_outputLiveness?.uninstall()'),
			'if teardown does not uninstall the liveness poll then every /performance remount leaves another 2.5s interval running forever - broken'
		);
		assert.ok(
			disarmBody.includes('clearAudioOutputHealth()'),
			'if teardown does not clear the output-health store then the bar under master volume keeps quoting a closed context - broken'
		);
	});

	it('armAudioContextWatchdog wires liveness snapshots into the merged output-health store (pin 93c82bb36eb7)', () => {
		const src = readFileSync(fileURLToPath(new URL('../../src/lib/rb/audio-context-instrumentation.ts', import.meta.url)), 'utf8');
		// Loaded lazily to keep it out of the first-paint chunk; that it really
		// installs is proven behaviorally in device-output-probe-arm.test.mjs.
		assert.ok(
			src.includes("import('$lib/rb/device-output-probe-browser')"),
			'if the OS output probe is not loaded at graph build then the bar cannot show device delivery faults - broken'
		);
		assert.ok(
			src.includes('onSnapshot: (snapshot) =>'),
			'if installOutputLiveness is not given onSnapshot then the bar under master volume never updates - broken'
		);
		assert.ok(
			src.includes('_deviceOutputProbe?.republish()'),
			'if browser liveness snapshots do not republish the merged store then the bar lags behind polls - broken'
		);
	});
});
