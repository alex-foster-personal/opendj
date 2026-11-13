// requirement: A11Y-14627
// [if] a performance modal opens without focus, ignores Escape from inside, leaks Tab or letters, or drops focus on close [then] this fails, [else stop].
/**
 * The real SampleHelperModal and IngestDropModal (both on /performance), client-mounted on the
 * quality-badge DOM shim. Window listeners are recorded so a key can be walked through window capture,
 * the element path and window bubble in browser order.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { bundlePerformanceModalsClient } from './mount-svelte-client.mjs';
import { installQualityBadgeDom } from './quality-badge-client-harness.mjs';

let client;
let gate;
let teardownDom = null;
const windowListeners = [];

// ----- DOM shim extensions: selectors, focus and tab order the focus trap reads -----

/** One compound selector (tag, .class, #id, [attr], [attr="v"], :disabled, :not(...)) against a shim element. */
function matchesCompound(el, compound) {
	let rest = compound;
	const tag = rest.match(/^[a-z][\w-]*/i);
	if (tag) {
		if (el.tagName.toLowerCase() !== tag[0].toLowerCase()) return false;
		rest = rest.slice(tag[0].length);
	}
	while (rest) {
		let m;
		if ((m = rest.match(/^:not\(([^)]*)\)/))) {
			if (matchesCompound(el, m[1])) return false;
		} else if ((m = rest.match(/^:disabled/))) {
			if (!(el.disabled === true || el.hasAttribute('disabled'))) return false;
		} else if ((m = rest.match(/^\.([\w-]+)/))) {
			if (!el.classList.contains(m[1])) return false;
		} else if ((m = rest.match(/^#([\w-]+)/))) {
			if (el.getAttribute('id') !== m[1]) return false;
		} else if ((m = rest.match(/^\[([\w-]+)(?:="([^"]*)")?\]/))) {
			const value = el.getAttribute(m[1]);
			if (value === null || (m[2] !== undefined && value !== m[2])) return false;
		} else {
			throw new Error(`test selector engine: unsupported selector part ${rest}`);
		}
		rest = rest.slice(m[0].length);
	}
	return true;
}

function matchesSelector(el, selector) {
	return selector.split(',').some((part) => matchesCompound(el, part.trim()));
}

const NATIVE_TAB_STOPS = new Set(['a', 'button', 'input', 'select', 'textarea']);

function extendShim() {
	const proto = globalThis.Element.prototype;
	proto.matches = function (selector) {
		return matchesSelector(this, selector);
	};
	proto.closest = function (selector) {
		for (let el = this; el; el = el.parentElement) if (matchesSelector(el, selector)) return el;
		return null;
	};
	proto.focus = function () {
		this.ownerDocument.activeElement = this;
	};
	proto.blur = function () {
		if (this.ownerDocument.activeElement === this)
			this.ownerDocument.activeElement = this.ownerDocument.body;
	};
	Object.defineProperty(proto, 'tabIndex', {
		configurable: true,
		get() {
			const attr = this.getAttribute('tabindex');
			if (attr !== null) return Number(attr);
			return NATIVE_TAB_STOPS.has(this.tagName.toLowerCase()) ? 0 : -1;
		}
	});
	proto.checkVisibility = () => true;
	globalThis.addEventListener = (type, fn, options) =>
		windowListeners.push({ type, fn, capture: options === true || options?.capture === true });
	globalThis.removeEventListener = (type, fn, options) => {
		const capture = options === true || options?.capture === true;
		const i = windowListeners.findIndex(
			(l) => l.type === type && l.fn === fn && l.capture === capture
		);
		if (i !== -1) windowListeners.splice(i, 1);
	};
}

before(async () => {
	teardownDom = installQualityBadgeDom();
	extendShim();
	client = await bundlePerformanceModalsClient();
	gate = await loadTypeScriptModule('src/lib/keyboard/modal-dialog-target.ts');
});

after(() => {
	delete globalThis.addEventListener;
	delete globalThis.removeEventListener;
	teardownDom?.();
});

// ----- event and mount helpers -----

/** Window capture, then the target's path to document, then window bubble: the browser's order. */
function fire(target, type, init = {}) {
	const event = {
		type,
		target,
		...init,
		defaultPrevented: false,
		cancelBubble: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {
			this.cancelBubble = true;
		},
		/** Svelte's delegated handlers walk this path; without it only the target's own handler runs. */
		composedPath() {
			const path = [];
			for (let node = target; node; node = node.parentNode) path.push(node);
			return [...path, globalThis];
		}
	};
	const onWindow = (capture) =>
		[...windowListeners].filter((l) => l.type === type && l.capture === capture);
	for (const l of onWindow(true)) if (!event.cancelBubble) l.fn(event);
	if (!event.cancelBubble) target.dispatchEvent(event);
	for (const l of onWindow(false)) if (!event.cancelBubble) l.fn(event);
	return event;
}

async function settle() {
	for (let i = 0; i < 6; i += 1) {
		await new Promise((resolve) => setTimeout(resolve, 0));
		client.flushSync();
	}
}

/** A focused opener button and a mount host under document.body, removed when the test ends. */
function stage(t) {
	const opener = document.createElement('button');
	const host = document.createElement('div');
	document.body.appendChild(opener);
	document.body.appendChild(host);
	opener.focus();
	t.after(() => {
		opener.remove();
		host.remove();
	});
	return { opener, host };
}

function mountInto(t, host, component, props) {
	const instance = client.mount(component, { target: host, props });
	t.after(() => client.unmount(instance));
	client.flushSync();
}

const button = (dialog, label) =>
	dialog.querySelectorAll('button').find((b) => b.textContent.trim() === label);

const DIALOGS = [
	{
		name: 'SampleHelperModal',
		async open(t) {
			const { opener, host } = stage(t);
			mountInto(t, host, client.SampleHelperModal, { open: true });
			await settle();
			return { opener, host };
		},
		first: (dialog) => dialog.querySelector('[data-testid="sample-helper-url"]'),
		last: (dialog) => dialog.querySelector('[data-testid="sample-helper-download"]')
	},
	{
		name: 'IngestDropModal',
		async open(t) {
			const { opener, host } = stage(t);
			mountInto(t, host, client.IngestDropModal, { onYoutubeLink: () => {} });
			const dataTransfer = { types: ['Files'], items: [], files: [{ name: 'track.mp3' }] };
			fire(document.body, 'drop', { dataTransfer });
			await settle();
			return { opener, host };
		},
		first: (dialog) => dialog.querySelector('input'),
		last: (dialog) => dialog.querySelector('[data-testid="ingest-run"]')
	}
];

async function openDialog(t, spec) {
	const { opener, host } = await spec.open(t);
	const dialog = host.querySelector('[role="dialog"]');
	assert.ok(dialog, `${spec.name} rendered no dialog`);
	assert.equal(dialog.getAttribute('aria-modal'), 'true', `${spec.name} is not marked aria-modal`);
	return { opener, host, dialog };
}

// ----- acceptance -----

test('A11Y-14627 either dialog moves focus to its first focusable control on open', async (t) => {
	// [if] a performance dialog opens and focus stays on the opener or lands off its first control [then] keyboard users start outside it, [else stop].
	for (const spec of DIALOGS) {
		const { opener, dialog } = await openDialog(t, spec);
		const first = spec.first(dialog);
		assert.ok(first, `${spec.name} has no first control`);
		assert.ok(document.activeElement !== opener, `${spec.name} left focus on the opener`);
		assert.ok(document.activeElement === first, `${spec.name} did not focus its first control`);
	}
});

test('A11Y-14627 Escape pressed on a button inside either dialog closes it', async (t) => {
	// [if] focus sits on a dialog button and Escape leaves the dialog open [then] it cannot be closed by keyboard, [else stop].
	for (const spec of DIALOGS) {
		const { host, dialog } = await openDialog(t, spec);
		const close = button(dialog, 'Close');
		assert.ok(close, `${spec.name} has no Close button`);
		close.focus();
		const event = fire(close, 'keydown', { key: 'Escape' });
		await settle();
		assert.equal(event.defaultPrevented, true, `${spec.name} did not consume Escape`);
		assert.equal(
			host.querySelector('[role="dialog"]'),
			null,
			`${spec.name} stayed open after Escape`
		);
	}
});

test('A11Y-14627 Tab on the last control wraps to the first control inside either dialog', async (t) => {
	// [if] Tab on the last dialog control walks focus out of the dialog [then] focus is not trapped, [else stop].
	for (const spec of DIALOGS) {
		const { dialog } = await openDialog(t, spec);
		const first = spec.first(dialog);
		const last = spec.last(dialog);
		assert.ok(last, `${spec.name} has no last control`);
		last.focus();
		const event = fire(last, 'keydown', { key: 'Tab', shiftKey: false });
		assert.equal(event.defaultPrevented, true, `${spec.name} let Tab leave the dialog`);
		assert.ok(
			document.activeElement === first,
			`${spec.name} Tab did not wrap to the first control`
		);
		fire(first, 'keydown', { key: 'Tab', shiftKey: true });
		assert.ok(
			document.activeElement === last,
			`${spec.name} Shift+Tab did not wrap back to the last control`
		);
	}
});

test('A11Y-14627 closing either dialog returns focus to the element that had it before opening', async (t) => {
	// [if] a dialog closes and focus does not land back on its opener [then] keyboard users lose their place, [else stop].
	for (const spec of DIALOGS) {
		const { opener, host, dialog } = await openDialog(t, spec);
		assert.ok(dialog.contains(document.activeElement), `${spec.name} never took focus`);
		fire(document.activeElement, 'keydown', { key: 'Escape' });
		await settle();
		assert.equal(host.querySelector('[role="dialog"]'), null, `${spec.name} did not close`);
		assert.ok(document.activeElement === opener, `${spec.name} did not return focus to its opener`);
	}
});

test('A11Y-14627 a letter key inside either dialog never reaches the performance shortcuts', async (t) => {
	// [if] a letter typed on a dialog button reaches a window shortcut listener or closes the dialog [then] it fires a deck command, [else stop].
	for (const spec of DIALOGS) {
		const { opener, host, dialog } = await openDialog(t, spec);
		const reached = [];
		const pageShortcut = (e) => reached.push(e.key);
		globalThis.addEventListener('keydown', pageShortcut);
		t.after(() => globalThis.removeEventListener('keydown', pageShortcut));
		const close = button(dialog, 'Close');
		close.focus();
		fire(close, 'keydown', { key: 'c' });
		globalThis.removeEventListener('keydown', pageShortcut);
		assert.deepEqual(reached, [], `${spec.name} let a letter reach a window bubble shortcut`);
		assert.equal(
			gate.isModalDialogTarget(close),
			true,
			`${spec.name} button is not gated off the capture-phase hotkeys`
		);
		assert.equal(
			gate.isModalDialogTarget(opener),
			false,
			'the gate also refused a key outside the dialog'
		);
		assert.ok(host.querySelector('[role="dialog"]'), `${spec.name} closed on a letter key`);
	}
});
