/**
 * Wheel sensitivity as a user setting: two independently persisted factors
 * (mouse and trackpad), a control for each in the settings panel, and the
 * proof that moving one actually retunes the controls rather than just
 * changing a number on screen.
 *
 * Issue #613. PR #599 shipped the 3x ratio and an agent bridge; it shipped no
 * control and no two-value persistence, so the ratio could only be changed
 * from devtools.
 *
 * HARNESS NOTE, load-bearing for how these assertions are written: the test
 * loader bundles each entry point with esbuild, so a module imported by two
 * entry points exists as two INSTANCES with two copies of the sensitivity
 * store. The cross-instance contract that is real in the app bundle (one
 * module instance) is therefore asserted through the two things that genuinely
 * are shared here - `localStorage`, and a fresh load reading it. Every test
 * below that spans two modules goes through one of those, never through an
 * in-process identity that only holds in the shipped bundle.
 *
 * Regression lines:
 * - if the catalog loses its control for either input kind then retuning the
 *   scroll wheel is a devtools job again, which is the whole of #613
 * - if the slider bounds stop coming from the validator then dragging to an end
 *   throws out of the settings panel
 * - if a panel write stops reaching localStorage then the value is gone on the
 *   next reload, the acceptance line this file exists for
 * - if the two kinds stop being written as two stored values then one slider
 *   moves both and #613's "two configs" is unmet
 * - if a configured factor stops reaching use:wheelAdjust then the panel changes
 *   a number and the dials ignore it
 * - if a configured factor stops reaching knob-control's page-level wheel then
 *   the shift-selected dial keeps the old sensitivity while every other control
 *   retunes, the exact hole PR #599 closed
 * - if the trackpad default stops being derived from the events-per-detent
 *   constant then that constant is a dead second source of truth
 */

import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STORAGE_KEY = 'mdt.rb.wheel-sensitivity.v1';
const WHEEL_MODULE = 'src/lib/rb/wheel-adjust.ts';
const KNOB_MODULE = 'src/lib/rb/knob-control.svelte.ts';

/** Both settings rows, paired with the store entry each one owns. */
const ROWS = [
	{ id: 'wheel_sensitivity.mouse', kind: 'mouse' },
	{ id: 'wheel_sensitivity.trackpad', kind: 'trackpad' }
];

let catalog;
let apply;
/** A separate instance from the copies esbuild inlines into catalog/apply/
 * knob-control. Only its own constants and its own action are meaningful. */
let wheel;
let store;

/** Minimal window + localStorage, installed BEFORE any module loads because
 * wheel-adjust reads its persisted factors at import time. */
function installFakeWindow() {
	const map = new Map();
	const listeners = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (key) => (map.has(key) ? map.get(key) : null),
			setItem: (key, value) => map.set(key, String(value)),
			removeItem: (key) => map.delete(key)
		},
		addEventListener: (type, fn) => listeners.set(type, fn),
		removeEventListener: (type) => listeners.delete(type)
	};
	globalThis.__listeners = listeners;
	return map;
}

function persistedBlob() {
	const raw = store.get(STORAGE_KEY);
	return raw === undefined ? null : JSON.parse(raw);
}

function wheelEvent(fields) {
	return { deltaX: 0, preventDefault() {}, stopPropagation() {}, ...fields };
}

/** One event out of a macOS trackpad two-finger burst: pixel deltaMode, a small
 * delta, and a wheelDeltaY that is NOT a multiple of 120. */
function trackpadTick(direction = 1, pixels = 2) {
	return wheelEvent({
		deltaMode: 0,
		deltaY: -pixels * direction,
		wheelDeltaY: pixels * 1.2 * direction
	});
}

before(async () => {
	store = installFakeWindow();
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	apply = await loadTypeScriptModule('src/lib/settings/apply.ts');
	wheel = await loadTypeScriptModule(WHEEL_MODULE);
	store.clear();
});

const defOf = (id) => catalog.SETTINGS_CATALOG.find((def) => def.id === id);

//-----------------------------------------------------------------------------
// the control exists, and is discoverable without devtools
//-----------------------------------------------------------------------------

describe('the settings panel control', () => {
	it('carries one implemented number control per input kind', () => {
		for (const { id } of ROWS) {
			const def = defOf(id);
			assert.ok(def, `${id} is not in the settings catalog at all`);
			assert.equal(def.implemented, true, `${id} is a grayed todo, not a control`);
			assert.equal(def.control.kind, 'number', `${id} has no usable control kind`);
			assert.equal(
				def.group,
				'performance',
				`${id} sits in group ${def.group}, away from the other mixer controls`
			);
		}
	});

	it('is findable through the real search, by the words a user would type', async () => {
		const { filterSettings } = await loadTypeScriptModule('src/lib/settings/search.ts');
		for (const query of ['wheel', 'trackpad', 'scroll', 'sensitivity', 'mouse']) {
			const { all } = filterSettings(query, { hideTodo: true, group: null });
			const ids = all.map((def) => def.id);
			assert.ok(
				ids.some((id) => id.startsWith('wheel_sensitivity.')),
				`searching ${JSON.stringify(query)} hides the wheel sensitivity rows entirely`
			);
		}
	});

	it('is allowlisted, so the control can actually write', () => {
		for (const { id } of ROWS) {
			assert.equal(apply.isAllowedSettingKey(id), true, `${id} is not allowlisted`);
			assert.doesNotThrow(() => apply.readSettingValue(id));
		}
	});

	it('takes its slider bounds from the validator, not from a second literal', () => {
		for (const { id } of ROWS) {
			const { control } = defOf(id);
			assert.equal(control.min, wheel.WHEEL_SENSITIVITY_MIN);
			assert.equal(control.max, wheel.WHEEL_SENSITIVITY_MAX);
			assert.equal(control.step, wheel.WHEEL_SENSITIVITY_STEP);
			assert.ok(
				control.min > 0,
				`${id} offers a factor of ${control.min} - zero makes the control inert`
			);
			// A slider position the setter would refuse is the failure this
			// guards: every value the range can land on must be writable.
			for (const end of [control.min, control.max]) {
				assert.doesNotThrow(
					() => apply.applySettingChange(id, String(end)),
					`the slider can land on ${end} but the mutator refuses it`
				);
			}
		}
	});

	it('offers the shipped default as the reset value, derived for the trackpad', () => {
		assert.equal(defOf('wheel_sensitivity.mouse').control.defaultValue, 1);
		assert.equal(
			defOf('wheel_sensitivity.trackpad').control.defaultValue,
			1 / wheel.WHEEL_TRACKPAD_EVENTS_PER_DETENT,
			'the trackpad default stopped being the reciprocal of the events-per-detent constant'
		);
		assert.equal(wheel.WHEEL_TRACKPAD_EVENTS_PER_DETENT, 3);
	});
});

//-----------------------------------------------------------------------------
// persistence, proven rather than assumed
//-----------------------------------------------------------------------------

describe('persistence across a reload', () => {
	it('writes the panel value to the key a reload reads back', async () => {
		store.clear();
		apply.applySettingChange('wheel_sensitivity.mouse', '1');
		apply.applySettingChange('wheel_sensitivity.trackpad', '0.2');

		assert.equal(
			apply.readSettingValue('wheel_sensitivity.trackpad'),
			'0.2',
			'the panel read back something other than what it just wrote'
		);
		assert.equal(
			persistedBlob().mouse,
			1,
			'writing the trackpad factor moved the mouse factor with it'
		);
		assert.equal(persistedBlob().trackpad, 0.2);

		const reloaded = await loadTypeScriptModule(WHEEL_MODULE);
		assert.equal(
			reloaded.wheelSensitivity().trackpad,
			0.2,
			'the reloaded module did not pick up the persisted factor'
		);
	});

	it('keeps mouse and trackpad as two independent stored values', async () => {
		store.clear();
		apply.applySettingChange('wheel_sensitivity.mouse', '2');
		apply.applySettingChange('wheel_sensitivity.trackpad', '0.2');

		assert.deepEqual(
			persistedBlob(),
			{ mouse: 2, trackpad: 0.2 },
			'the two input kinds are not two independently persisted values'
		);

		const reloaded = await loadTypeScriptModule(WHEEL_MODULE);
		assert.deepEqual(reloaded.wheelSensitivity(), { mouse: 2, trackpad: 0.2 });
	});

	it('survives a reload after a reset back to the shipped defaults', async () => {
		store.clear();
		for (const { id } of ROWS) {
			apply.applySettingChange(id, String(defOf(id).control.defaultValue));
		}
		assert.deepEqual(persistedBlob(), {
			mouse: 1,
			trackpad: 1 / wheel.WHEEL_TRACKPAD_EVENTS_PER_DETENT
		});

		const reloaded = await loadTypeScriptModule(WHEEL_MODULE);
		assert.deepEqual(reloaded.wheelSensitivity(), { ...reloaded.WHEEL_SENSITIVITY });
	});
});

//-----------------------------------------------------------------------------
// fail-loud input handling
//-----------------------------------------------------------------------------

describe('the panel refuses what the validator refuses', () => {
	it('rejects a factor outside the bounds instead of storing it', () => {
		store.clear();
		assert.throws(
			() => apply.applySettingChange('wheel_sensitivity.mouse', '0'),
			RangeError
		);
		assert.throws(
			() => apply.applySettingChange('wheel_sensitivity.mouse', '-3'),
			RangeError
		);
		assert.throws(
			() =>
				apply.applySettingChange(
					'wheel_sensitivity.mouse',
					String(wheel.WHEEL_SENSITIVITY_MAX + 1)
				),
			RangeError
		);
		assert.throws(
			() => apply.applySettingChange('wheel_sensitivity.mouse', String(wheel.WHEEL_SENSITIVITY_MIN / 2)),
			RangeError
		);
		assert.equal(persistedBlob(), null, 'a refused value was persisted anyway');
	});

	it('rejects a non-numeric payload rather than coercing it', () => {
		store.clear();
		assert.throws(
			() => apply.applySettingChange('wheel_sensitivity.trackpad', 'wide open'),
			/expects a number/
		);
		assert.throws(
			() => apply.applySettingChange('wheel_sensitivity.trackpad', ''),
			/expects a number/
		);
		assert.throws(
			() => apply.applySettingChange('wheel_sensitivity.trackpad', true),
			/expects a number/
		);
		assert.equal(persistedBlob(), null);
	});
});

//-----------------------------------------------------------------------------
// agent-native parity
//-----------------------------------------------------------------------------

describe('agent-native parity for each control', () => {
	it('exposes a programmatic equivalent that writes the same store', () => {
		store.clear();
		const bridge = window.__mdtWheelSensitivity;
		assert.equal(typeof bridge.get, 'function');
		assert.equal(typeof bridge.set, 'function');
		assert.equal(typeof bridge.reset, 'function');
		assert.equal(typeof bridge.defaults, 'function');

		bridge.set('mouse', 2);
		bridge.set('trackpad', 0.15);
		assert.deepEqual(bridge.get(), { mouse: 2, trackpad: 0.15 });
		assert.deepEqual(
			persistedBlob(),
			{ mouse: 2, trackpad: 0.15 },
			'the bridge did not persist to the key the settings control writes'
		);

		bridge.reset();
		assert.deepEqual(bridge.get(), bridge.defaults());
		assert.equal(persistedBlob(), null, 'reset left a stored blob behind');
	});
});

//-----------------------------------------------------------------------------
// the configured factor actually reaches both wheel seams
//-----------------------------------------------------------------------------

describe('a configured factor reaches both wheel seams', () => {
	it('scales the use:wheelAdjust action', () => {
		wheel.resetWheelSensitivity();
		wheel.setWheelSensitivity('trackpad', 0.1);

		let value = 0.5;
		let handler = null;
		const node = {
			addEventListener: (type, fn) => {
				if (type === 'wheel') handler = fn;
			},
			removeEventListener: () => {
				handler = null;
			}
		};
		wheel.wheelAdjust(node, {
			step: wheel.WHEEL_STEP.fader,
			get: () => value,
			set: (next) => {
				value = next;
			}
		});

		handler(trackpadTick(1));
		const configured = value - 0.5;
		assert.ok(
			Math.abs(configured - wheel.WHEEL_STEP.fader * 0.1) < 1e-12,
			`use:wheelAdjust moved ${configured}, the configured factor asked for ` +
				`${wheel.WHEEL_STEP.fader * 0.1}`
		);
		assert.ok(
			configured < wheel.WHEEL_STEP.fader * wheel.WHEEL_SENSITIVITY.trackpad,
			'the action ignored the configured factor and used the default'
		);

		wheel.resetWheelSensitivity();
		store.clear();
	});

	it('scales the page-level wheel in knob-control, from the persisted blob', async () => {
		// The write goes through the settings mutator, then a fresh load of the
		// wheel seam reads storage - which is exactly what a reload does. This
		// is the only honest way to span the two here: in the shipped bundle
		// they are one instance, in this harness they are two.
		store.clear();
		apply.applySettingChange('wheel_sensitivity.trackpad', '0.25');
		assert.equal(persistedBlob().trackpad, 0.25);

		globalThis.HTMLElement = class FakeHTMLElement {
			constructor(tagName, { isContentEditable = false } = {}) {
				this.tagName = tagName;
				this.isContentEditable = isContentEditable;
			}
		};
		const knobs = await loadTypeScriptModule(KNOB_MODULE);

		let dial = 0.5;
		knobs.registerKnob({
			id: '1:low',
			getValue: () => dial,
			setValue: (next) => {
				dial = next;
			}
		});
		knobs.knobUi.selectedId = '1:low';

		const handler = globalThis.__listeners.get('wheel');
		assert.equal(typeof handler, 'function', 'knob-control never bound its page-level wheel');

		handler(trackpadTick(1));
		const configured = dial - 0.5;
		assert.ok(
			Math.abs(configured - knobs.KNOB_CFG.scrollStep * 0.25) < 1e-12,
			`the page-level wheel moved ${configured}; the persisted factor asked for ` +
				`${knobs.KNOB_CFG.scrollStep * 0.25}`
		);
		const defaultTravel = knobs.KNOB_CFG.scrollStep * wheel.WHEEL_SENSITIVITY.trackpad;
		assert.ok(
			Math.abs(configured - defaultTravel) > 1e-12,
			'the page-level wheel ignored the persisted factor and took the default step'
		);

		store.clear();
		installFakeWindow();
	});
});
