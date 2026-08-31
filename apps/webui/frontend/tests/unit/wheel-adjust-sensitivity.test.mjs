/**
 * Trackpad-vs-mouse wheel sensitivity for every wheel-adjustable control.
 *
 * Regression lines:
 * - if a macOS trackpad burst and a mouse detent stop moving a fader by
 *   comparable amounts then the global sensitivity scaling has regressed and
 *   every dial and fader is hypersensitive on the Air again
 * - if detectWheelInputKind stops reading wheelDeltaY quantization then Chrome
 *   and Safari lose the only signal that separates the two devices on macOS
 * - if a non-pixel deltaMode stops meaning mouse then Firefox notched wheels
 *   get scaled down as if they were trackpads
 * - if setWheelSensitivity accepts a non-positive or absurd factor then a typo
 *   silently makes every control inert or unusable
 * - if a malformed stored blob resets instead of throwing then a corrupted
 *   config masquerades as "never configured"
 */

import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

/** Minimal localStorage + window so the module's persistence path is live.
 * Installed BEFORE the module loads, because it reads storage at import. */
function installFakeWindow() {
	const store = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	return store;
}

before(async () => {
	installFakeWindow();
	mod = await loadTypeScriptModule('src/lib/rb/wheel-adjust.ts');
});

//-----------------------------------------------------------------------------
// synthetic events
//-----------------------------------------------------------------------------

function wheelEvent(fields) {
	return { deltaX: 0, preventDefault() {}, stopPropagation() {}, ...fields };
}

/**
 * One detent of a notched mouse wheel as macOS Chrome reports it: pixel
 * deltaMode, deltaY of 100 per notch, and wheelDeltaY quantized to 120.
 * direction +1 raises (browsers send a negative deltaY for scroll-up).
 */
function mouseDetent(direction = 1, notches = 1) {
	return wheelEvent({
		deltaMode: 0,
		deltaY: -100 * notches * direction,
		wheelDeltaY: 120 * notches * direction
	});
}

/**
 * One event out of a macOS trackpad two-finger burst: pixel deltaMode, a small
 * delta, and a wheelDeltaY that is NOT a multiple of 120.
 */
function trackpadTick(direction = 1, pixels = 2) {
	return wheelEvent({
		deltaMode: 0,
		deltaY: -pixels * direction,
		wheelDeltaY: pixels * 1.2 * direction
	});
}

/** A control wired through the real action, with a captured wheel listener. */
function mountControl(step, initial) {
	let value = initial;
	let handler = null;
	const node = {
		addEventListener: (type, fn) => {
			if (type === 'wheel') handler = fn;
		},
		removeEventListener: () => {
			handler = null;
		}
	};
	const action = mod.wheelAdjust(node, {
		step,
		get: () => value,
		set: (next) => {
			value = next;
		}
	});
	return {
		action,
		fire: (event) => handler(event),
		get value() {
			return value;
		}
	};
}

//-----------------------------------------------------------------------------
// detection
//-----------------------------------------------------------------------------

describe('detectWheelInputKind', () => {
	it('reads a quantized wheelDeltaY as a mouse (Chromium / WebKit on macOS)', () => {
		assert.equal(mod.detectWheelInputKind(mouseDetent(1)), 'mouse');
		assert.equal(mod.detectWheelInputKind(mouseDetent(-1)), 'mouse');
		assert.equal(mod.detectWheelInputKind(mouseDetent(1, 2)), 'mouse');
	});

	it('reads a non-quantized wheelDeltaY as a trackpad', () => {
		assert.equal(mod.detectWheelInputKind(trackpadTick(1, 2)), 'trackpad');
		assert.equal(mod.detectWheelInputKind(trackpadTick(-1, 7)), 'trackpad');
	});

	it('reads a fractional trackpad delta as a trackpad', () => {
		const event = wheelEvent({ deltaMode: 0, deltaY: -0.8333, wheelDeltaY: 1 });
		assert.equal(mod.detectWheelInputKind(event), 'trackpad');
	});

	it('reads a line-mode event as a mouse (Firefox notched wheel)', () => {
		const event = wheelEvent({ deltaMode: 1, deltaY: -3 });
		assert.equal(mod.detectWheelInputKind(event), 'mouse');
	});

	it('reads pixel mode with no wheelDeltaY as a trackpad (Firefox trackpad)', () => {
		const event = wheelEvent({ deltaMode: 0, deltaY: -12.5 });
		assert.equal(mod.detectWheelInputKind(event), 'trackpad');
	});

	it('documents the known false negative: a flick landing on an exact 120', () => {
		// Accepted limit, asserted so it stays visible rather than surprising a
		// later reader. Costs one coarser step inside a burst of many.
		const event = wheelEvent({ deltaMode: 0, deltaY: -100, wheelDeltaY: 120 });
		assert.equal(mod.detectWheelInputKind(event), 'mouse');
	});
});

//-----------------------------------------------------------------------------
// scaling
//-----------------------------------------------------------------------------

describe('scaledWheelStep', () => {
	it('leaves the mouse step at its declared value', () => {
		assert.equal(mod.scaledWheelStep(mod.WHEEL_STEP.fader, 'mouse'), mod.WHEEL_STEP.fader);
	});

	it('scales the trackpad step down by the configured factor', () => {
		assert.equal(
			mod.scaledWheelStep(mod.WHEEL_STEP.fader, 'trackpad'),
			mod.WHEEL_STEP.fader * mod.WHEEL_SENSITIVITY.trackpad
		);
	});

	it('defaults trackpad to 3x less sensitive than mouse (the maintainer, 31 Aug 2026)', () => {
		assert.equal(mod.WHEEL_SENSITIVITY.mouse, 1);
		assert.equal(mod.WHEEL_TRACKPAD_EVENTS_PER_DETENT, 3);
	});
});

//-----------------------------------------------------------------------------
// the acceptance test: equivalent gestures move a fader comparably
//-----------------------------------------------------------------------------

describe('equivalent physical gestures', () => {
	const EPSILON = 1e-12;

	it('moves a level fader by a comparable amount on trackpad and mouse', () => {
		const mouse = mountControl(mod.WHEEL_STEP.fader, 0.5);
		mouse.fire(mouseDetent(1));
		const mouseTravel = mouse.value - 0.5;

		const trackpad = mountControl(mod.WHEEL_STEP.fader, 0.5);
		for (let i = 0; i < mod.WHEEL_TRACKPAD_EVENTS_PER_DETENT; i += 1) {
			trackpad.fire(trackpadTick(1));
		}
		const trackpadTravel = trackpad.value - 0.5;

		assert.ok(mouseTravel > 0, `mouse detent moved nothing (${mouseTravel})`);
		assert.ok(
			Math.abs(trackpadTravel - mouseTravel) < EPSILON,
			`trackpad burst moved ${trackpadTravel}, mouse detent moved ${mouseTravel} - ` +
				'an equivalent gesture must travel the same distance on both devices'
		);
	});

	it('holds for every control step, not just the fader', () => {
		for (const [name, step] of Object.entries(mod.WHEEL_STEP)) {
			const mouse = mountControl(step, 0.5);
			mouse.fire(mouseDetent(1));
			const trackpad = mountControl(step, 0.5);
			for (let i = 0; i < mod.WHEEL_TRACKPAD_EVENTS_PER_DETENT; i += 1) {
				trackpad.fire(trackpadTick(1));
			}
			assert.ok(
				Math.abs(trackpad.value - mouse.value) < EPSILON,
				`${name}: trackpad landed at ${trackpad.value}, mouse at ${mouse.value}`
			);
		}
	});

	it('leaves single-detent mouse behavior exactly as it was', () => {
		const control = mountControl(mod.WHEEL_STEP.knob, 0.5);
		control.fire(mouseDetent(1));
		assert.equal(control.value, 0.5 + mod.WHEEL_STEP.knob);
	});

	it('still clamps to 0..1 under a long trackpad burst', () => {
		const control = mountControl(mod.WHEEL_STEP.fader, 0.98);
		for (let i = 0; i < 200; i += 1) control.fire(trackpadTick(1));
		assert.equal(control.value, 1);
		for (let i = 0; i < 400; i += 1) control.fire(trackpadTick(-1));
		assert.equal(control.value, 0);
	});

	it('ignores the wheel entirely on a disabled control', () => {
		let value = 0.5;
		let handler = null;
		const node = {
			addEventListener: (type, fn) => {
				if (type === 'wheel') handler = fn;
			},
			removeEventListener: () => {}
		};
		mod.wheelAdjust(node, {
			step: mod.WHEEL_STEP.fader,
			get: () => value,
			set: (next) => {
				value = next;
			},
			disabled: true
		});
		handler(trackpadTick(1));
		handler(mouseDetent(1));
		assert.equal(value, 0.5);
	});
});

//-----------------------------------------------------------------------------
// config + agent-native parity
//-----------------------------------------------------------------------------

describe('wheel sensitivity config', () => {
	it('round-trips a configured factor and applies it to the step', () => {
		mod.setWheelSensitivity('trackpad', 0.25);
		assert.equal(mod.wheelSensitivity().trackpad, 0.25);
		assert.equal(mod.scaledWheelStep(0.04, 'trackpad'), 0.01);
		mod.resetWheelSensitivity();
		assert.equal(mod.wheelSensitivity().trackpad, mod.WHEEL_SENSITIVITY.trackpad);
	});

	it('rejects a non-positive, non-finite or absurd factor', () => {
		assert.throws(() => mod.setWheelSensitivity('trackpad', 0), RangeError);
		assert.throws(() => mod.setWheelSensitivity('trackpad', -1), RangeError);
		assert.throws(() => mod.setWheelSensitivity('trackpad', Number.NaN), RangeError);
		assert.throws(
			() => mod.setWheelSensitivity('trackpad', mod.WHEEL_SENSITIVITY_MAX + 1),
			RangeError
		);
		assert.throws(() => mod.setWheelSensitivity('joystick', 1), TypeError);
	});

	it('persists through localStorage so the choice survives a reload', () => {
		mod.setWheelSensitivity('trackpad', 0.5);
		const raw = window.localStorage.getItem('mdt.rb.wheel-sensitivity.v1');
		assert.equal(JSON.parse(raw).trackpad, 0.5);
		mod.resetWheelSensitivity();
		assert.equal(window.localStorage.getItem('mdt.rb.wheel-sensitivity.v1'), null);
	});

	it('exposes window.__mdtWheelSensitivity for headless agents', () => {
		const bridge = window.__mdtWheelSensitivity;
		assert.equal(typeof bridge.get, 'function');
		assert.equal(typeof bridge.set, 'function');
		assert.equal(typeof bridge.reset, 'function');
		bridge.set('trackpad', 0.2);
		assert.equal(bridge.get().trackpad, 0.2);
		bridge.reset();
		assert.equal(bridge.get().trackpad, bridge.defaults().trackpad);
	});
});

describe('wheel sensitivity storage policy', () => {
	it('throws loudly on a malformed stored blob instead of resetting', async () => {
		const store = installFakeWindow();
		store.set('mdt.rb.wheel-sensitivity.v1', JSON.stringify({ trackpad: -5 }));
		await assert.rejects(
			() => loadTypeScriptModule('src/lib/rb/wheel-adjust.ts'),
			/malformed blob/
		);
		installFakeWindow();
	});

	it('treats a missing key as first run and uses the defaults', async () => {
		installFakeWindow();
		const fresh = await loadTypeScriptModule('src/lib/rb/wheel-adjust.ts');
		assert.deepEqual(fresh.wheelSensitivity(), { ...fresh.WHEEL_SENSITIVITY });
	});
});
