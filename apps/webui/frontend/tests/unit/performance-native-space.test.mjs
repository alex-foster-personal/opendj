/**
 * @pytest.mark.requirement A11Y-01
 * Issue #12281: Space through the REAL installed /performance listener
 * (installPerformanceHotkeys), not the router predicate alone. Only the IPC
 * send is replaced (fixtures/performance-native-space-stub-ipc.ts) so the
 * transport toggle is observable; the native activation itself is the
 * browser's, pinned by tests/e2e/performance-native-space.spec.ts.
 *
 * Regression lines:
 * - if Space on a focused button sends a transport command then the button loses its own activation
 * - if Space on a focused checkbox is default-prevented then the box never toggles
 * - if Space on inert chrome is not default-prevented then the page scrolls under the DJ
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const STUB_IPC = `${FRONTEND_ROOT}/tests/unit/fixtures/performance-native-space-stub-ipc.ts`;

class FakeElement {}
class FakeHTMLElement extends FakeElement {}

const saved = {};
const listeners = new Map();
let m;
let uninstall;

/** A focused element with only the attributes the listener reads. */
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

/** Dispatches one plain Space keydown at `target` through the installed capture listener. */
function pressSpace(target) {
	const event = {
		type: 'keydown',
		code: 'Space',
		key: ' ',
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

function transportSends() {
	return m.sent.filter((c) => c.type === 'load_play_intent' || c.type === 'play');
}

before(async () => {
	for (const key of ['window', 'Element', 'HTMLElement']) saved[key] = globalThis[key];
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = FakeHTMLElement;
	// Module-load listeners (page lifecycle and the like) are not under test,
	// so only what installPerformanceHotkeys registers is kept.
	let recording = false;
	globalThis.window = {
		addEventListener: (type, fn, opts) => {
			if (recording) listeners.set(type, { fn, capture: opts?.capture === true });
		},
		removeEventListener: (type) => listeners.delete(type),
		// prefs.svelte reads saved settings at import; an empty store is a first boot.
		localStorage: {
			getItem: () => null,
			setItem: () => {},
			removeItem: () => {}
		}
	};
	m = await loadTypeScriptModule('tests/unit/fixtures/performance-native-space-entry.ts', {
		alias: { '$lib/rb/performance-ipc.svelte': STUB_IPC }
	});
	recording = true;
	uninstall = m.installPerformanceHotkeys();
	recording = false;
	// Capture phase runs before any focused control's own handler can stop it.
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
	// A deck with a pending load-play is a transport target Space would toggle.
	m.beginPendingLoadPlay(1, false);
	m.sent.length = 0;
});

test('[if] Space hits a focused button via the installed listener [then] no transport send and no preventDefault, [else stop].', async () => {
	const event = pressSpace(focused('BUTTON'));
	await Promise.resolve();
	assert.equal(event.defaultPrevented, false, 'the browser must still activate the button');
	assert.deepEqual(transportSends(), []);
});

test('[if] Space hits a focused checkbox via the installed listener [then] no transport send and no preventDefault, [else stop].', async () => {
	const event = pressSpace(focused('INPUT', { type: 'checkbox' }));
	await Promise.resolve();
	assert.equal(event.defaultPrevented, false, 'the browser must still toggle the checkbox');
	assert.deepEqual(transportSends(), []);
});

test('[if] Space hits inert performance chrome via the installed listener [then] one transport send and preventDefault, [else stop].', async () => {
	const event = pressSpace(focused('DIV'));
	await Promise.resolve();
	assert.equal(event.defaultPrevented, true, 'an unprevented Space scrolls the page');
	assert.equal(transportSends().length, 1);
	assert.equal(transportSends()[0].type, 'load_play_intent');
	assert.equal(transportSends()[0].desired_play, true);
});
