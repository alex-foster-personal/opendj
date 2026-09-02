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
 * [if] a second trigger lands inside the cooldown [then] no second cycle (a
 *   flapping device must not loop the graph)
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

function harness({ playing = true, state = 'running' } = {}) {
	const calls = [];
	const ctx = {
		state,
		async suspend() { calls.push('suspend'); this.state = 'suspended'; },
		async resume() { calls.push('resume'); this.state = 'running'; }
	};
	let now = 0;
	const timers = [];
	const effects = {
		pushToast: (m, k) => calls.push(`toast:${k}`),
		recordPerfEvent: (kind) => calls.push(`perf:${kind}`),
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
		for (const t of timers.splice(0).filter((t) => t.at <= now)) t.fn();
		await new Promise((r) => setImmediate(r));
		await new Promise((r) => setImmediate(r));
	}
	return { ctx, effects, calls, mediaDevices, fireDeviceChange: () => listeners.forEach((h) => h()), advance, isPlaying: () => playing };
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

	it('a second trigger inside the cooldown does not cycle again', async () => {
		const h = harness();
		mod.installOutputRebind(h.ctx, h.effects, h.isPlaying, h.mediaDevices);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.equal(h.calls.filter((c) => c === 'suspend').length, 1, 'if the cooldown does not hold then a flapping device loops the graph - broken');
		await h.advance(mod.REBIND_COOLDOWN_MS);
		h.fireDeviceChange();
		await h.advance(mod.REBIND_DEBOUNCE_MS);
		assert.equal(h.calls.filter((c) => c === 'suspend').length, 2, 'after the cooldown a new change must cycle again');
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
