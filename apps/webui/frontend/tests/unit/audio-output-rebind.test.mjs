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
});

describe('wiring (source guards; the engine and reporter are rune-bound)', () => {
	const report = readFileSync(fileURLToPath(new URL('../../src/lib/rb/presentation-clock-report.ts', import.meta.url)), 'utf8');
	const instr = readFileSync(fileURLToPath(new URL('../../src/lib/rb/audio-context-instrumentation.ts', import.meta.url)), 'utf8');

	it('the stall edge calls noteOutputStall', () => {
		assert.ok(report.includes('noteOutputStall(deck)'), 'if the stall reporter does not call noteOutputStall then the phone-call trigger is wired to nothing - broken');
	});

	it('the rebind is installed next to the context watchdog', () => {
		assert.ok(instr.includes('installOutputRebind('), 'if armAudioContextWatchdog does not install the rebind then no device change ever re-binds - broken');
	});
});
