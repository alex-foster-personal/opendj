/**
 * Headphone monitor liveness (detached HTMLAudioElement + MediaStream sink).
 *
 * [if] currentTime stops advancing for >= OUTPUT_STALL_MS while monitoring
 *   [then] verdict stalled with one error toast and perf row
 * [if] the stall persists through LIVENESS_DEAD_POLLS polls
 *   [then] verdict dead, then dead-escalated at LIVENESS_ESCALATE_POLLS
 * [if] currentTime advances again [then] verdict ok with one restored toast
 * [if] the element fires error [then] verdict dead without waiting for polls
 * [if] devicechange reports the sink vanished [then] verdict dead
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function elementHarness({
	playing = true,
	currentTime = 0.1,
	readyState = 4,
	frozenTime = false,
	error = null,
	ended = false
} = {}) {
	const calls = [];
	const snapshots = [];
	let nowMs = 0;
	let mediaTime = currentTime;
	let devicePresent = true;
	let intervalFn = null;
	const listeners = new Map();
	const element = {
		get currentTime() {
			return mediaTime;
		},
		readyState,
		paused: !playing,
		ended,
		error,
		addEventListener(type, listener) {
			const bucket = listeners.get(type) ?? [];
			bucket.push(listener);
			listeners.set(type, bucket);
		},
		removeEventListener(type, listener) {
			const bucket = listeners.get(type) ?? [];
			listeners.set(type, bucket.filter((fn) => fn !== listener));
		},
		emit(type) {
			for (const fn of listeners.get(type) ?? []) fn();
		},
		setMediaTime(value) {
			if (!frozenTime) mediaTime = value;
		},
		freeze() {
			frozenTime = true;
		},
		unfreeze(nextTime) {
			frozenTime = false;
			if (nextTime !== undefined) mediaTime = nextTime;
		}
	};
	let deviceHandler = null;
	const effects = {
		pushToast: (m, k) => calls.push(`toast:${k}:${m.slice(0, 28)}`),
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
		watchDeviceChanges: (handler) => {
			deviceHandler = handler;
			return () => {
				deviceHandler = null;
			};
		}
	};
	return {
		element,
		effects,
		calls,
		snapshots,
		poll: () => intervalFn && intervalFn(),
		isMonitoring: () => true,
		isDevicePresent: () => devicePresent,
		setDevicePresent(value) {
			devicePresent = value;
		},
		deviceChange: () => deviceHandler && deviceHandler(),
		advance(ms, advanceTime = true) {
			nowMs += ms;
			if (advanceTime && !frozenTime) mediaTime += ms / 1000;
		}
	};
}

describe('installHeadphoneOutputLiveness', () => {
	let mod;
	before(async () => {
		mod = await loadTypeScriptModule('src/lib/rb/audio-output-liveness.ts');
	});

	it('frozen currentTime for >= OUTPUT_STALL_MS => stalled + one error toast', () => {
		const h = elementHarness();
		const live = mod.installHeadphoneOutputLiveness(
			h.element,
			h.effects,
			h.isMonitoring,
			h.isDevicePresent
		);
		h.poll();
		h.element.freeze();
		h.advance(mod.OUTPUT_STALL_MS + 1, false);
		h.poll();
		assert.equal(live.verdict(), 'stalled');
		assert.equal(h.calls.filter((c) => c === 'perf:headphone-output-stalled:error').length, 1);
		assert.equal(h.calls.filter((c) => c.startsWith('toast:error:NO HEADPHONE OUTPUT:')).length, 1);
	});

	it('sustained stall escalates dead then dead-escalated', () => {
		const h = elementHarness();
		const live = mod.installHeadphoneOutputLiveness(
			h.element,
			h.effects,
			h.isMonitoring,
			h.isDevicePresent
		);
		h.poll();
		h.element.freeze();
		for (let i = 0; i < 5; i++) {
			h.advance(mod.LIVENESS_POLL_MS, false);
			h.poll();
		}
		assert.equal(live.verdict(), 'dead-escalated');
		assert.equal(h.calls.filter((c) => c === 'perf:headphone-output-dead:error').length, 1);
		assert.equal(h.calls.filter((c) => c === 'perf:headphone-output-dead-persistent:error').length, 1);
	});

	it('advancing currentTime recovers to ok with one restored toast', () => {
		const h = elementHarness();
		const live = mod.installHeadphoneOutputLiveness(
			h.element,
			h.effects,
			h.isMonitoring,
			h.isDevicePresent
		);
		h.poll();
		h.element.freeze();
		h.advance(mod.OUTPUT_STALL_MS + 1, false);
		h.poll();
		assert.equal(live.verdict(), 'stalled');
		h.element.unfreeze(h.element.currentTime + 0.5);
		h.advance(100);
		h.poll();
		assert.equal(live.verdict(), 'ok');
		assert.equal(h.calls.filter((c) => c === 'toast:info:Headphone output restored').length, 1);
	});

	it('element error event => dead without waiting for stall timing', () => {
		const h = elementHarness();
		const live = mod.installHeadphoneOutputLiveness(
			h.element,
			h.effects,
			h.isMonitoring,
			h.isDevicePresent
		);
		h.poll();
		h.element.error = { message: 'sink failed' };
		h.element.emit('error');
		assert.equal(live.verdict(), 'dead');
		assert.equal(h.calls.filter((c) => c === 'perf:headphone-output-dead:error').length, 1);
	});

	it('devicechange with vanished sink => dead', () => {
		const h = elementHarness();
		const live = mod.installHeadphoneOutputLiveness(
			h.element,
			h.effects,
			h.isMonitoring,
			h.isDevicePresent
		);
		h.poll();
		h.setDevicePresent(false);
		h.deviceChange();
		assert.equal(live.verdict(), 'dead');
		assert.equal(h.calls.filter((c) => c === 'perf:headphone-output-device-vanished:error').length, 1);
	});

	it('not monitoring => idle and silent', () => {
		const h = elementHarness();
		mod.installHeadphoneOutputLiveness(
			h.element,
			h.effects,
			() => false,
			h.isDevicePresent
		);
		for (let i = 0; i < 6; i++) {
			h.advance(mod.LIVENESS_POLL_MS, false);
			h.poll();
		}
		assert.deepEqual(h.calls, []);
		assert.equal(h.snapshots.at(-1)?.verdict, 'idle');
	});
});

describe('headphoneLivenessAlertText', () => {
	let headphones;
	before(async () => {
		headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	});

	it('maps broken verdicts to operator-facing alert copy', () => {
		assert.equal(headphones.headphoneLivenessAlertText('stalled'), 'Headphone output not producing sound');
		assert.equal(headphones.headphoneLivenessAlertText('dead'), 'Headphone output not producing sound');
		assert.match(headphones.headphoneLivenessAlertText('dead-escalated'), /re-select/);
		assert.equal(headphones.headphoneLivenessAlertText('ok'), null);
		assert.equal(headphones.headphoneLivenessAlertText('idle'), null);
	});
});
