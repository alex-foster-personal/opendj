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
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function harness({ playing = true, state = 'running', latency = 0 } = {}) {
	const calls = [];
	const ctx = { state, outputLatency: latency, baseLatency: 0.0058, sinkId: '', getOutputTimestamp: () => ({ contextTime: 1, performanceTime: 2 }) };
	let intervalFn = null;
	const effects = {
		pushToast: (m, k) => calls.push(`toast:${k}:${m.slice(0, 20)}`),
		recordPerfEvent: (kind, _m, sev) => calls.push(`perf:${kind}:${sev}`),
		setInterval: (fn) => { intervalFn = fn; return 'h'; },
		clearInterval: () => { intervalFn = null; }
	};
	return { ctx, effects, calls, poll: () => intervalFn && intervalFn(), isPlaying: () => playing };
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
		h.poll();
		assert.equal(live.verdict(), 'dead');
		assert.deepEqual(h.calls, ['perf:audio-output-dead:error', 'toast:error:NO AUDIO OUTPUT: the'],
			'if a dead output is not an error state then silence stays silent - broken');
		// The re-bind request crosses a module boundary the TS loader instantiates
		// separately per test, so it is pinned at source level: the dead verdict
		// must call noteOutputStall, which audio-output-rebind.test.mjs proves cycles
		// the context.
		const src = readFileSync(fileURLToPath(new URL('../../src/lib/rb/audio-output-liveness.ts', import.meta.url)), 'utf8');
		const deadBranch = src.slice(src.indexOf("verdict = 'dead';"), src.indexOf("} else if (deadPolls === LIVENESS_ESCALATE_POLLS)"));
		assert.ok(deadBranch.includes('noteOutputStall(0)'), 'if the dead verdict does not request a re-bind then nothing tries to recover - broken');
	});

	it('four zero polls = sticky reload escalation, once', () => {
		const h = harness();
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		for (let i = 0; i < 5; i++) h.poll();
		assert.equal(live.verdict(), 'dead-escalated');
		assert.equal(h.calls.filter((c) => c.startsWith('perf:audio-output-dead-persistent')).length, 1);
		assert.equal(h.calls.filter((c) => c.startsWith('toast:error')).length, 2, 'exactly one alarm and one escalation, not a toast per poll');
	});

	it('latency returning flips to ok with one restored toast', () => {
		const h = harness();
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		h.poll(); h.poll();
		h.ctx.outputLatency = 0.19;
		h.poll(); h.poll();
		assert.equal(live.verdict(), 'ok');
		assert.equal(h.calls.filter((c) => c === 'toast:info:Audio output restore').length, 1);
	});

	it('nothing playing, or a non-running context, never alarms', () => {
		const idle = harness({ playing: false });
		mod.installOutputLiveness(idle.ctx, idle.effects, idle.isPlaying);
		for (let i = 0; i < 6; i++) idle.poll();
		assert.deepEqual(idle.calls, []);
		const suspended = harness({ state: 'suspended' });
		mod.installOutputLiveness(suspended.ctx, suspended.effects, suspended.isPlaying);
		for (let i = 0; i < 6; i++) suspended.poll();
		assert.deepEqual(suspended.calls, []);
	});

	it('healthy latency throughout = ok and silent', () => {
		const h = harness({ latency: 0.19 });
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		for (let i = 0; i < 6; i++) h.poll();
		assert.equal(live.verdict(), 'ok');
		assert.deepEqual(h.calls, []);
	});

	it('snapshot carries the fields an agent needs', () => {
		const h = harness({ latency: 0.192 });
		const live = mod.installOutputLiveness(h.ctx, h.effects, h.isPlaying);
		h.poll();
		const s = live.snapshot();
		assert.deepEqual(Object.keys(s).sort(), ['base_latency_ms', 'output_context_time_s', 'output_latency_ms', 'sink_id', 'state', 'verdict']);
		assert.equal(s.output_latency_ms, 192);
	});
});

describe('wiring (source guard)', () => {
	it('armAudioContextWatchdog installs the liveness poll and exposes __mdtAudioOutput', () => {
		const src = readFileSync(fileURLToPath(new URL('../../src/lib/rb/audio-context-instrumentation.ts', import.meta.url)), 'utf8');
		assert.ok(src.includes('installOutputLiveness('), 'if the liveness poll is not installed at graph build then the detector is code nobody runs - broken');
		assert.ok(src.includes('__mdtAudioOutput'), 'if the snapshot is not exposed then an agent cannot read output health - broken');
	});

	it('disarmContextInstrumentation uninstalls the liveness poll', () => {
		const src = readFileSync(fileURLToPath(new URL('../../src/lib/rb/audio-context-instrumentation.ts', import.meta.url)), 'utf8');
		// `armAudioContextWatchdog` also calls `_outputLiveness?.uninstall()` (it
		// clears the previous handle before arming a fresh one), so a loose
		// `disarmContextInstrumentation[\s\S]*?...` regex still matches there even
		// if the teardown's own call is deleted. Delimit the body by brace-counting
		// from the function's opening `{` so this can only match inside
		// `disarmContextInstrumentation` itself.
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
	});
});
