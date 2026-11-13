/**
 * @pytest.mark.requirement UX-TOAST-02
 * Issue #12264: a /performance toast can be dismissed from the keyboard.
 *
 * The dismiss control is a native `<button type="button">`, so the browser
 * itself activates it on Enter and Space. What this file pins without a
 * browser: the rendered control (real ToastStack through svelte/server, real
 * toast store), that no /performance key router or keymap preset claims Enter
 * or Space while that button has focus (a router that called preventDefault
 * would cancel the native activation), and that the dismissal the button runs
 * clears the toast it names. What a pass does NOT cover: a real key press in a
 * browser, which needs an e2e run.
 *
 * Regression one-liners:
 *   - if the dismiss control stops being a native button then broken
 *   - if the dismiss control loses its accessible name then broken
 *   - if the dismiss button nests inside the copy button then broken
 *   - if the toast loses role="status" then broken
 *   - if a router or preset claims Enter or Space on a focused button then broken
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as ToastStack } from '$lib/components/rb/ToastStack.svelte';",
	"export { toasts, pushToast, dismissToast } from '$lib/stores.svelte';",
	"export { resolvePerformanceShortcutAction } from '$lib/rb/performance-shortcut-routing';",
	"export { isNativeInteractiveTarget } from '$lib/rb/performance-hotkeys-target';",
	"export { handleKeymapKeydown, refreshKeyboardPrefFromServer } from '$lib/keyboard/keymap-runtime.svelte';",
	"export { KEYMAP_PRESET_IDS } from '$lib/keyboard/presets';",
	"export { uiPrefs } from '$lib/rb/performance-ipc.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;
const saved = { Element: globalThis.Element, HTMLElement: globalThis.HTMLElement };

// Stand-ins for the DOM classes the routers test with instanceof; no DOM here.
class FakeElement {
	constructor(tagName, attrs = {}) {
		this.tagName = tagName;
		this.attrs = attrs;
		this.isContentEditable = false;
	}
	hasAttribute(name) {
		return Object.hasOwn(this.attrs, name);
	}
	getAttribute(name) {
		return this.hasAttribute(name) ? this.attrs[name] : null;
	}
	closest() {
		return null;
	}
}
class FakeHTMLElement extends FakeElement {}

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = FakeHTMLElement;
});

after(() => {
	globalThis.Element = saved.Element;
	globalThis.HTMLElement = saved.HTMLElement;
	clearToasts();
});

function clearToasts() {
	for (const toast of [...(mod?.toasts ?? [])]) mod.dismissToast(toast.logId);
}

function showToast(message) {
	clearToasts();
	mod.pushToast(message, 'warn', 5000);
	return mod.toasts[0].logId;
}

function renderStack() {
	return mod.render(mod.ToastStack, { props: { items: [...mod.toasts] } }).body;
}

function dismissButtonTag(markup, logId) {
	const tag = markup.match(new RegExp(`<button[^>]*data-toast-dismiss="${logId}"[^>]*>`))?.[0];
	assert.ok(tag, `a dismiss control is rendered for ${logId}`);
	return tag;
}

/** Button nesting depth at `index`; 0 means the control is not inside another button. */
function buttonDepthAt(markup, index) {
	let depth = 0;
	for (const match of markup.slice(0, index).matchAll(/<button\b|<\/button>/g)) {
		depth += match[0] === '</button>' ? -1 : 1;
	}
	return depth;
}

function keyEvent(code, key, target) {
	return { code, key, target, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false };
}

function dismissButtonTarget(logId) {
	return new FakeHTMLElement('BUTTON', {
		type: 'button',
		'data-toast-dismiss': logId,
		'aria-label': `Dismiss message ${logId}`
	});
}

/** Run the real keymap runtime gate under `preset`; repeat stops a claimed key before it dispatches. */
function keymapVerdict(preset, event, overrides = {}) {
	const saved = mod.uiPrefs.keyboard;
	try {
		mod.uiPrefs.keyboard = { preset, overrides };
		mod.refreshKeyboardPrefFromServer();
		let prevented = false;
		const keydown = {
			...event,
			repeat: true,
			timeStamp: 0,
			preventDefault: () => (prevented = true)
		};
		return { claimed: mod.handleKeymapKeydown(keydown, {}), prevented };
	} finally {
		mod.uiPrefs.keyboard = saved;
		mod.refreshKeyboardPrefFromServer();
	}
}

function assertKeyLeftToButton(code, key, logId) {
	const event = keyEvent(code, key, dismissButtonTarget(logId));
	assert.equal(mod.isNativeInteractiveTarget(event.target), true, 'the button owns its keys');
	assert.equal(mod.resolvePerformanceShortcutAction(event), null, `legacy router leaves ${code}`);
	for (const preset of mod.KEYMAP_PRESET_IDS) {
		const verdict = keymapVerdict(preset, event);
		assert.deepEqual(
			verdict,
			{ claimed: false, prevented: false },
			`preset ${preset} leaves ${code}`
		);
	}
}

function assertDismissClears(logId) {
	assert.equal(mod.dismissToast(logId), true, 'the dismissal finds the shown toast');
	assert.equal(renderStack().includes(`data-toast-dismiss="${logId}"`), false);
}

/** [if] a dismissible toast is shown [then] it has a named native dismiss button outside the copy button and role=status, [else stop]. */
test('a shown toast renders a named native dismiss button', () => {
	const logId = showToast('Deck 2 load failed');
	const markup = renderStack();
	const tag = dismissButtonTag(markup, logId);
	assert.match(tag, /type="button"/);
	assert.match(tag, new RegExp(`aria-label="Dismiss message ${logId}"`));
	assert.equal(buttonDepthAt(markup, markup.indexOf(tag)), 0, 'not nested in the copy button');
	const toastTag = markup.match(new RegExp(`<div[^>]*data-toast-id="${logId}"[^>]*>`))?.[0] ?? '';
	assert.match(toastTag, /role="status"/, 'the toast still announces as a status');
	assert.doesNotMatch(toastTag, /tabindex=|role="button"/, 'the toast box is not a fake control');
	clearToasts();
});

/** [if] the focused dismiss control receives Enter [then] no router or preset claims it and the dismissal clears that toast, [else stop]. */
test('Enter on the dismiss button is left to the button and dismisses', () => {
	const logId = showToast('Enter dismiss');
	dismissButtonTag(renderStack(), logId);
	assertKeyLeftToButton('Enter', 'Enter', logId);
	assertDismissClears(logId);
});

/** [if] the focused dismiss control receives Space [then] transport yields to the button and the dismissal clears that toast, [else stop]. */
test('Space on the dismiss button is left to the button and dismisses', () => {
	const logId = showToast('Space dismiss');
	dismissButtonTag(renderStack(), logId);
	assertKeyLeftToButton('Space', ' ', logId);
	const chrome = keyEvent('Space', ' ', new FakeHTMLElement('DIV'));
	assert.deepEqual(
		mod.resolvePerformanceShortcutAction(chrome),
		{ kind: 'space', quantize: false },
		'Space on page chrome still toggles play, so the yield is specific to the button'
	);
	// A remap hands Space to the keymap runtime: it claims chrome, never the button.
	const remap = { 'transport.toggle': ['Space'] };
	const onButton = keyEvent('Space', ' ', dismissButtonTarget(logId));
	const left = { claimed: false, prevented: false };
	assert.deepEqual(keymapVerdict('opendj', onButton, remap), left, 'remapped Space yields');
	const claimed = { claimed: true, prevented: true };
	assert.deepEqual(keymapVerdict('opendj', chrome, remap), claimed, 'remapped Space claims chrome');
	assertDismissClears(logId);
});
