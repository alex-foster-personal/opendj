// requirement: KEYB-14276
/**
 * #13428: focus restoration by selector-tree identity (selector-focus-restore.ts).
 *
 * The real installer, `selectorNode` action, node registry and snapshot run together; only the
 * DOM is a small fake tree (elements, controls, document.activeElement and the focus events).
 *
 * Regression lines:
 * - if a focused control whose selector node re-renders does not get focus back on the same control slot then broken
 * - if a vanished node's focus does not fall back to its nearest surviving ancestor's first non-text control then broken
 * - if focus with no surviving node, or on a control outside every selector node, is moved by a re-render then broken
 */
import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const listeners = new Map();
const doc = { activeElement: null, body: null };

function _dispatch(type, target) {
	for (const listener of listeners.get(type) ?? []) listener({ type, target });
}

class FakeElement {
	constructor(tagName, attrs = {}) {
		this.tagName = tagName;
		this.attrs = new Map(Object.entries(attrs));
		this.type = attrs.type ?? '';
		this.children = [];
		this.parentElement = null;
	}
	get isConnected() {
		let el = this;
		while (el.parentElement !== null) el = el.parentElement;
		return el === doc.body;
	}
	append(...kids) {
		for (const kid of kids) {
			kid.parentElement = this;
			this.children.push(kid);
		}
		return this;
	}
	/** Detach from the page; the browser drops focus to <body> when it held the focused element. */
	remove() {
		const wasFocused = this.contains(doc.activeElement);
		this.parentElement.children = this.parentElement.children.filter((kid) => kid !== this);
		this.parentElement = null;
		if (wasFocused) doc.activeElement = doc.body;
	}
	contains(other) {
		for (let el = other; el !== null; el = el.parentElement) if (el === this) return true;
		return false;
	}
	getAttribute(name) {
		return this.attrs.get(name) ?? null;
	}
	setAttribute(name, value) {
		this.attrs.set(name, value);
	}
	removeAttribute(name) {
		this.attrs.delete(name);
	}
	toggleAttribute(name, force) {
		if (force) this.attrs.set(name, '');
		else this.attrs.delete(name);
	}
	/** Only the selector the module asks for: the `data-sel-node` attribute. */
	closest(selector) {
		assert.equal(selector, '[data-sel-node]');
		for (let el = this; el !== null; el = el.parentElement)
			if (el.attrs.has('data-sel-node')) return el;
		return null;
	}
	_descendants() {
		return this.children.flatMap((kid) => [kid, ...kid._descendants()]);
	}
	/** Controls in DOM order; `[data-sel-primary]` alone narrows to the marked primary. */
	querySelectorAll(selector) {
		const all = this._descendants();
		if (selector === '[data-sel-primary]')
			return all.filter((el) => el.attrs.has('data-sel-primary'));
		return all.filter(
			(el) => ['BUTTON', 'INPUT'].includes(el.tagName) || el.attrs.get('role') === 'slider'
		);
	}
	querySelector(selector) {
		return this.querySelectorAll(selector)[0] ?? null;
	}
	getBoundingClientRect() {
		return { x: 0, y: 0, width: 100, height: 100 };
	}
	focus() {
		const previous = doc.activeElement;
		doc.activeElement = this;
		if (previous !== null && previous !== doc.body) _dispatch('focusout', previous);
		_dispatch('focusin', this);
	}
}

let m;
let uninstall = () => {};
const mounted = new Set();

/** A selector node element registered through the real action; `unmount` detaches and destroys it. */
function node(id, kind, ...kids) {
	const el = new FakeElement('DIV').append(...kids);
	const action = m.selectorNode(el, { id, kind, enabled: true });
	const handle = {
		el,
		unmount: () => {
			if (!mounted.delete(handle)) return;
			if (el.parentElement !== null) el.remove();
			action.destroy();
		}
	};
	mounted.add(handle);
	return handle;
}

const button = () => new FakeElement('BUTTON');
const slider = () => new FakeElement('DIV', { role: 'slider' });
const textInput = () => new FakeElement('INPUT', { type: 'text' });

/** One registry batch settles (its microtask), then the focus restorer has run. */
const settle = () => new Promise((resolve) => setImmediate(resolve));

/** body > decks (section) > mixer.ch1 (group: name field, trim leaf, fader leaf). */
function mountChannel() {
	const trim = node('mixer.ch1.trim', 'leaf', slider());
	const faderControls = [button(), slider()];
	const fader = node('mixer.ch1.fader', 'leaf', ...faderControls);
	const channel = node('mixer.ch1', 'group', textInput(), trim.el, fader.el);
	const decks = node('decks', 'section', channel.el);
	doc.body.append(decks.el);
	return { decks, channel, trim, fader, faderControls };
}

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/selector-focus-restore-entry.ts');
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = FakeElement;
	globalThis.getComputedStyle = () => ({ display: 'block' });
	globalThis.document = doc;
	globalThis.window = {
		addEventListener: (type, fn) => listeners.set(type, [...(listeners.get(type) ?? []), fn]),
		removeEventListener: (type, fn) =>
			listeners.set(
				type,
				(listeners.get(type) ?? []).filter((f) => f !== fn)
			)
	};
});

afterEach(async () => {
	uninstall();
	for (const handle of [...mounted]) handle.unmount();
	await settle();
});

function freshPage() {
	doc.body = new FakeElement('BODY');
	doc.activeElement = doc.body;
	uninstall = m.installSelectorFocusRestore();
}

test('a focused selector node that survives a refresh gets focus back on the same control slot', async () => {
	/** [if] the focused fader slider's node re-renders under the same id [then] the new slider holds focus, [else stop]. */
	freshPage();
	const page = mountChannel();
	page.faderControls[1].focus();
	assert.deepEqual(m.markSelectorFocus(page.faderControls[1]), {
		chain: ['mixer.ch1.fader', 'mixer.ch1', 'decks'],
		slot: 1
	});
	page.fader.unmount();
	_dispatch('focusout', page.faderControls[1]);
	const freshControls = [button(), slider()];
	const fresh = node('mixer.ch1.fader', 'leaf', ...freshControls);
	page.channel.el.append(fresh.el);
	assert.equal(doc.activeElement, doc.body, 'the re-render dropped focus to the body first');
	await settle();
	assert.equal(doc.activeElement, freshControls[1]);
});

test('a focused node gone after a refresh falls back to its nearest surviving ancestor, never a text field', async () => {
	/** [if] the focused fader node unmounts for good [then] focus lands on mixer.ch1's first non-text control, [else stop]. */
	freshPage();
	const page = mountChannel();
	page.faderControls[0].focus();
	page.fader.unmount();
	await settle();
	const trimSlider = page.trim.el.children[0];
	assert.equal(doc.activeElement, trimSlider);
	assert.deepEqual(
		m.selectorFocusRestoreNode(
			{ chain: ['mixer.ch1.fader', 'mixer.ch1', 'decks'], slot: 0 },
			() => false
		),
		null
	);
	for (const handle of [...mounted]) handle.unmount();
	await settle();
	assert.equal(doc.activeElement, doc.body, 'no surviving ancestor: focus stays on the body');
});

test('focus on a control outside every selector node is never moved or restored', async () => {
	/** [if] focus sits on a non-selector control while nodes re-render, or that control unmounts [then] focus is not moved, [else stop]. */
	freshPage();
	const page = mountChannel();
	const outside = button();
	doc.body.append(outside);
	assert.equal(m.markSelectorFocus(outside), null);
	page.faderControls[1].focus();
	outside.focus();
	page.fader.unmount();
	await settle();
	assert.equal(
		doc.activeElement,
		outside,
		'a re-render does not take focus from a non-selector control'
	);
	outside.remove();
	page.channel.el.append(node('mixer.ch1.fader', 'leaf', button()).el);
	await settle();
	assert.equal(
		doc.activeElement,
		doc.body,
		'a removed non-selector control is not restored into the tree'
	);
	page.trim.el.children[0].focus();
	doc.activeElement = doc.body;
	_dispatch('focusout', page.trim.el.children[0]);
	await settle();
	page.trim.unmount();
	await settle();
	assert.equal(doc.activeElement, doc.body, 'a deliberate blur cancels the restore');
});
