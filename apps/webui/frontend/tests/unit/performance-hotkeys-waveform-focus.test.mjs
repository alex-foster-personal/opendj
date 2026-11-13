// requirement: A11Y-02
/**
 * Issue #12274: the pointer-only waveform focus exception, driven through the
 * REAL installed /performance listener (installPerformanceHotkeys) and the
 * real target predicates, not a router call alone. Only the IPC send is
 * replaced (fixtures/performance-hotkeys-waveform-focus-stub-ipc.ts) so a
 * dispatch is observable. tests/e2e/performance-hotkeys-waveform-focus.spec.ts
 * proves the same three lines against the running app with real focus.
 *
 * Regression lines:
 * - if a click-focused waveform seek canvas swallows Space or M then every hotkey dies until focus moves
 * - if Space on a focused native button dispatches or is default-prevented then the button never activates
 * - if a bare letter typed into a text entry dispatches or is default-prevented then typing breaks
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const STUB_IPC = `${FRONTEND_ROOT}/tests/unit/fixtures/performance-hotkeys-waveform-focus-stub-ipc.ts`;
// Bare letters /performance claims on page chrome: C cue, V beat sync,
// K master tempo, M comment pin, P pitch range.
const CLAIMED_LETTERS = ['c', 'v', 'k', 'm', 'p'];

class FakeElement {}
class FakeHTMLElement extends FakeElement {}

const saved = {};
const listeners = new Map();
let m;
let uninstall;

/** A focused element exposing only what the listener and predicates read. */
function focused(tagName, attrs = {}) {
	const el = new FakeHTMLElement();
	el.tagName = tagName;
	el.isContentEditable = false;
	el.type = attrs.type ?? '';
	el.getAttribute = (name) => attrs[name] ?? null;
	el.hasAttribute = (name) => name in attrs;
	el.closest = () => null;
	return el;
}

/** WaveRow.svelte's seek canvas, with or without the pointer-only marker. */
function waveformSeekCanvas({ marked = true } = {}) {
	return focused('CANVAS', {
		role: 'slider',
		tabindex: '-1',
		'aria-label': 'deck 1 waveform seek',
		...(marked ? { [m.POINTER_ONLY_HOTKEY_PASSTHROUGH_ATTR]: '' } : {})
	});
}

/** Dispatches one plain keydown at `target` through the installed capture listener. */
function press(target, { code, key }) {
	const event = {
		type: 'keydown',
		code,
		key,
		target,
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		shiftKey: false,
		repeat: false,
		timeStamp: 1000,
		defaultPrevented: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {}
	};
	const onKey = listeners.get('keydown');
	assert.ok(onKey !== undefined, 'installPerformanceHotkeys must register a keydown listener');
	onKey.fn(event);
	return event;
}

const SPACE = { code: 'Space', key: ' ' };
const letter = (key) => ({ code: `Key${key.toUpperCase()}`, key });

before(async () => {
	for (const key of ['window', 'Element', 'HTMLElement']) saved[key] = globalThis[key];
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = FakeHTMLElement;
	// Module-load listeners are not under test; only what installPerformanceHotkeys registers is kept.
	let recording = false;
	globalThis.window = {
		addEventListener: (type, fn, opts) => {
			if (recording) listeners.set(type, { fn, capture: opts?.capture === true });
		},
		removeEventListener: (type) => listeners.delete(type),
		// prefs.svelte reads saved settings at import; an empty store is a first boot.
		localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} }
	};
	m = await loadTypeScriptModule(
		'tests/unit/fixtures/performance-hotkeys-waveform-focus-entry.ts',
		{
			alias: { '$lib/rb/performance-ipc.svelte': STUB_IPC }
		}
	);
	recording = true;
	uninstall = m.installPerformanceHotkeys();
	recording = false;
	assert.equal(listeners.get('keydown')?.capture, true, 'keydown must be a capture listener');
});

after(() => {
	uninstall?.();
	for (const [key, value] of Object.entries(saved)) {
		if (value === undefined) delete globalThis[key];
		else globalThis[key] = value;
	}
});

beforeEach(() => {
	// A pending load-play gives Space a transport send with no engine behind it.
	m.beginPendingLoadPlay(1, false);
	m.sent.length = 0;
	// M arms placement only once the feedback probe has answered ok.
	m.feedbackState.availability = 'ok';
	m.feedbackState.placementArmed = false;
});

test('[if] the click-focused waveform seek canvas gets Space or M [then] both hotkeys still dispatch, [else stop].', async () => {
	const canvas = waveformSeekCanvas();
	assert.equal(
		m.isNativeInteractiveTarget(canvas),
		false,
		'the marked canvas is not a native control'
	);
	assert.equal(m.isTextEntryTarget(canvas), false, 'the marked canvas is not text entry');

	const space = press(canvas, SPACE);
	await Promise.resolve();
	assert.equal(space.defaultPrevented, true, 'the listener must claim Space on the canvas');
	assert.deepEqual(
		m.sent.map((c) => c.type),
		['load_play_intent'],
		'Space on the canvas must toggle transport'
	);

	const pin = press(canvas, letter('m'));
	assert.equal(pin.defaultPrevented, true, 'the listener must claim M on the canvas');
	assert.equal(m.feedbackState.placementArmed, true, 'M on the canvas must arm pin placement');

	// The same canvas without the marker keeps the conservative default, so the
	// pass above is the marker's doing and not a predicate that claims everything.
	m.sent.length = 0;
	const unmarked = press(waveformSeekCanvas({ marked: false }), SPACE);
	await Promise.resolve();
	assert.equal(unmarked.defaultPrevented, false);
	assert.deepEqual(m.sent, [], 'an unmarked role=slider tabindex=-1 canvas keeps Space');
});

test('[if] a focused native button gets Space [then] no command dispatches and the event is not prevented, [else stop].', async () => {
	const button = focused('BUTTON', { type: 'button' });
	assert.equal(m.isNativeInteractiveTarget(button), true, 'a button owns its own Space');

	const event = press(button, SPACE);
	await Promise.resolve();
	assert.equal(event.defaultPrevented, false, 'the browser must still activate the button');
	assert.deepEqual(m.sent, [], 'Space on a button must not reach deck transport');
});

test('[if] a text entry gets a bare claimed letter [then] no command dispatches and typing goes through, [else stop].', async () => {
	// Positive control: page chrome claims every one of these letters, so the
	// text-entry pass below is the gate's doing.
	for (const key of CLAIMED_LETTERS) {
		assert.equal(press(focused('DIV'), letter(key)).defaultPrevented, true, `${key} on chrome`);
	}
	m.sent.length = 0;
	m.feedbackState.placementArmed = false;

	const input = focused('INPUT', { type: 'text' });
	assert.equal(m.isTextEntryTarget(input), true, 'a text input is text entry');
	for (const key of CLAIMED_LETTERS) {
		const event = press(input, letter(key));
		assert.equal(event.defaultPrevented, false, `${key} must type into the input`);
	}
	await Promise.resolve();
	assert.deepEqual(m.sent, [], 'no performance command may dispatch from typing');
	assert.equal(m.feedbackState.placementArmed, false, 'typing m must not arm a pin');
});
