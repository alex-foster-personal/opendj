/**
 * One hover tooltip. The custom layer reads `title` and parks it on
 * `data-tip` while the tip is shown, so WKWebView has no live title to draw
 * a second native tooltip. Pointer leave restores `title`.
 *
 * A MutationObserver in the shape of rust-inert (and the real watchRustInert
 * hold) must not write `title` back during the park, and must not loop.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

class El {
	constructor(tag) {
		this.tagName = String(tag).toUpperCase();
		this.parentElement = null;
		this.children = [];
		this.attrs = new Map();
		this.listeners = [];
		this.hidden = false;
		this.textContent = '';
		this.style = {};
		this.disabled = false;
		this.offsetWidth = 80;
		this.offsetHeight = 18;
		const classes = new Set();
		this.classList = {
			add: (c) => classes.add(c),
			remove: (c) => classes.delete(c),
			toggle: (c, force) => {
				const on = force === undefined ? !classes.has(c) : force;
				if (on) classes.add(c);
				else classes.delete(c);
				return on;
			},
			contains: (c) => classes.has(c)
		};
	}

	appendChild(child) {
		child.parentElement = this;
		this.children.push(child);
		return child;
	}

	remove() {
		const p = this.parentElement;
		if (p === null) return;
		p.children = p.children.filter((c) => c !== this);
		this.parentElement = null;
	}

	setAttribute(name, value) {
		this.attrs.set(name, String(value));
		noteAttr(this, name);
	}

	getAttribute(name) {
		return this.attrs.has(name) ? this.attrs.get(name) : null;
	}

	hasAttribute(name) {
		return this.attrs.has(name);
	}

	removeAttribute(name) {
		if (!this.attrs.has(name)) return;
		this.attrs.delete(name);
		noteAttr(this, name);
	}

	get dataset() {
		const el = this;
		return new Proxy(
			{},
			{
				get(_t, prop) {
					if (typeof prop !== 'string') return undefined;
					const attr = `data-${prop.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`;
					return el.getAttribute(attr) ?? undefined;
				},
				set(_t, prop, value) {
					const attr = `data-${String(prop).replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`;
					el.setAttribute(attr, value);
					return true;
				},
				deleteProperty(_t, prop) {
					const attr = `data-${String(prop).replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`;
					el.removeAttribute(attr);
					return true;
				}
			}
		);
	}

	matches(selector) {
		return selector.split(',').some((part) => this._one(part.trim()));
	}

	_one(selector) {
		if (selector.startsWith('[') && selector.endsWith(']')) {
			const body = selector.slice(1, -1);
			const eq = body.indexOf('=');
			if (eq === -1) return this.hasAttribute(body);
			const key = body.slice(0, eq);
			const val = body.slice(eq + 1).replace(/^"|"$/g, '');
			return this.getAttribute(key) === val;
		}
		return this.tagName.toLowerCase() === selector.toLowerCase();
	}

	closest(selector) {
		let n = this;
		while (n) {
			if (n.matches(selector)) return n;
			n = n.parentElement;
		}
		return null;
	}

	contains(other) {
		let n = other;
		while (n) {
			if (n === this) return true;
			n = n.parentElement;
		}
		return false;
	}

	querySelectorAll(selector) {
		const out = [];
		const walk = (node) => {
			for (const child of node.children) {
				if (child.matches(selector)) out.push(child);
				walk(child);
			}
		};
		walk(this);
		return out;
	}

	getBoundingClientRect() {
		return { left: 12, top: 40, width: 48, height: 20, right: 60, bottom: 60 };
	}

	addEventListener(type, fn, capture) {
		this.listeners.push({ type, fn, capture: capture === true });
	}

	removeEventListener(type, fn) {
		this.listeners = this.listeners.filter((l) => l.type !== type || l.fn !== fn);
	}

	dispatchEvent(event) {
		event.target = this;
		const chain = [];
		let n = this;
		while (n) {
			chain.push(n);
			n = n.parentElement;
		}
		chain.push(doc);
		for (const node of chain) {
			for (const l of node.listeners) {
				if (l.type === event.type && l.capture) l.fn(event);
			}
		}
		for (const node of [...chain].reverse()) {
			for (const l of node.listeners) {
				if (l.type === event.type && !l.capture) l.fn(event);
			}
		}
		return true;
	}
}

const observers = [];

function noteAttr(el, name) {
	for (const obs of observers) {
		if (!obs.options?.attributes) continue;
		if (obs.options.attributeFilter && !obs.options.attributeFilter.includes(name)) continue;
		if (obs.root !== el && !obs.root.contains?.(el)) continue;
		obs.pending.push({ target: el, attributeName: name });
	}
	queueMicrotask(flushObservers);
}

function flushObservers() {
	for (const obs of observers) {
		if (obs.pending.length === 0) continue;
		const batch = obs.pending;
		obs.pending = [];
		obs.cb(batch);
	}
}

class MO {
	constructor(cb) {
		this.cb = cb;
		this.pending = [];
		this.root = null;
		this.options = null;
	}

	observe(root, options) {
		this.root = root;
		this.options = options;
		observers.push(this);
	}

	disconnect() {
		const i = observers.indexOf(this);
		if (i >= 0) observers.splice(i, 1);
	}
}

const doc = new El('document');
doc.body = new El('body');
doc.documentElement = new El('html');
doc.createElement = (tag) => new El(tag);

let hover;
let rust;
let uninstall = () => {};

before(async () => {
	globalThis.Element = El;
	globalThis.HTMLElement = El;
	globalThis.MutationObserver = MO;
	globalThis.document = doc;
	globalThis.window = globalThis;
	globalThis.location = { search: '?engine=rust' };
	globalThis.localStorage = { getItem: () => null, setItem: () => {} };
	globalThis.innerWidth = 1024;
	globalThis.innerHeight = 768;
	hover = await loadTypeScriptModule('src/lib/ui/single-hover-tooltip.ts');
	rust = await loadTypeScriptModule('tests/unit/fixtures/rust-inert-entry.ts');
	uninstall = hover.installSingleHoverTooltip(doc);
});

after(() => {
	uninstall();
});

function pointer(type, target, relatedTarget = null) {
	target.dispatchEvent({ type, relatedTarget, target });
}

function tip() {
	return doc.body.children.find((c) => c.getAttribute('role') === 'tooltip' && c.hidden !== true);
}

test('hovering help text shows our tooltip and parks title, then restores it', async () => {
	const button = doc.createElement('button');
	button.setAttribute('title', 'Beat grid: 128.0 from the file');
	doc.body.appendChild(button);
	pointer('pointerover', button);
	await Promise.resolve();
	assert.equal(button.hasAttribute('title'), false);
	assert.equal(button.getAttribute('data-tip'), 'Beat grid: 128.0 from the file');
	assert.equal(button.hasAttribute('data-tip-parked'), true);
	assert.match(button.getAttribute('aria-describedby') ?? '', /single-hover-tip/);
	const shown = tip();
	assert.ok(shown, 'our tooltip is shown');
	assert.equal(shown.textContent, 'Beat grid: 128.0 from the file');
	pointer('pointerout', button, doc.body);
	await Promise.resolve();
	assert.equal(button.getAttribute('title'), 'Beat grid: 128.0 from the file');
	assert.equal(button.hasAttribute('data-tip-parked'), false);
	assert.equal(tip(), undefined);
	button.remove();
});

test('[if] a readout title changes under the pointer [then] leave keeps the new title, not the parked one, [else stop]', async () => {
	// Deck grid adjust (GRIDEDIT-6235): clicking the button changes its "Now:" readout
	// while it is hovered; restore used to write the stale parked copy back.
	const button = doc.createElement('button');
	button.setAttribute('title', 'Grid adjust. Now: 0 ms');
	doc.body.appendChild(button);
	pointer('pointerover', button);
	await Promise.resolve();
	assert.equal(button.getAttribute('data-tip'), 'Grid adjust. Now: 0 ms');
	button.setAttribute('title', 'Grid adjust. Now: +30 ms');
	pointer('pointerout', button, doc.body);
	await Promise.resolve();
	assert.equal(button.getAttribute('title'), 'Grid adjust. Now: +30 ms', 'the component wrote it last');
	assert.equal(button.hasAttribute('data-tip'), false);
	assert.equal(button.hasAttribute('data-tip-parked'), false);
	button.remove();
});

test('an element with no help text gets no tooltip', async () => {
	const button = doc.createElement('button');
	doc.body.appendChild(button);
	pointer('pointerover', button);
	await Promise.resolve();
	assert.equal(tip(), undefined);
	assert.equal(button.hasAttribute('title'), false);
	button.remove();
});

test('the rust-inert title observer does not restore title or loop during the park', async () => {
	const root = doc.createElement('div');
	const button = doc.createElement('button');
	button.setAttribute('data-performance-control', 'loop-interval');
	button.setAttribute('title', 'Loop length');
	root.appendChild(button);
	doc.body.appendChild(root);
	const watch = rust.watchRustInert(root);
	await Promise.resolve();
	await Promise.resolve();
	assert.equal(button.getAttribute('title'), rust.RUST_INERT_TITLE);
	let observerCalls = 0;
	const guard = new MO(() => {
		observerCalls += 1;
		if (observerCalls > 8) throw new Error('title mutations looped');
	});
	guard.observe(button, { attributes: true, attributeFilter: ['title'] });
	pointer('pointerover', button);
	await Promise.resolve();
	await Promise.resolve();
	assert.equal(button.hasAttribute('title'), false, 'parked title stays off while our tooltip is up');
	assert.equal(button.getAttribute('data-tip'), rust.RUST_INERT_TITLE);
	assert.ok(observerCalls < 8);
	pointer('pointerout', button, doc.body);
	await Promise.resolve();
	await Promise.resolve();
	assert.equal(button.getAttribute('title'), rust.RUST_INERT_TITLE);
	watch.stop();
	await Promise.resolve();
	assert.equal(button.getAttribute('title'), 'Loop length');
	guard.disconnect();
	root.remove();
});

/** The box a user can SEE: shown and not the clipped screen-reader copy. */
function visibleTip() {
	const shown = tip();
	if (shown === undefined) return undefined;
	return shown.classList.contains('single-hover-tip-sr') ? undefined : shown;
}

function build(tag, attrs = {}, parent = doc.body) {
	const el = doc.createElement(tag);
	for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
	parent.appendChild(el);
	return el;
}

test('a titled button inside a hover-card host shows no second box (AutoPlay shape)', async () => {
	// Tue 6 Oct 2026: the AutoPlay card AND "AutoPlay OFF - ..." showed at once.
	const wrap = build('span', { 'data-custom-tip': '' });
	const button = build('button', { title: 'AutoPlay OFF - No automatic next-track handoff.' }, wrap);
	pointer('pointerover', button);
	await Promise.resolve();
	assert.equal(visibleTip(), undefined, 'the card is the one explainer');
	assert.equal(button.hasAttribute('title'), false, 'native tooltip parked too');
	pointer('pointerout', button, doc.body);
	await Promise.resolve();
	wrap.remove();
});

test('a titled ANCESTOR of a hover-card host shows no second box (compatible-filter shape)', async () => {
	const label = build('label', { title: 'Show only tracks compatible with the reference deck' });
	const host = build('div', { 'data-custom-tip': '' }, label);
	const input = build('input', {}, host);
	pointer('pointerover', input);
	await Promise.resolve();
	assert.equal(visibleTip(), undefined);
	assert.equal(label.hasAttribute('title'), false);
	pointer('pointerout', input, doc.body);
	await Promise.resolve();
	assert.equal(label.getAttribute('title'), 'Show only tracks compatible with the reference deck');
	label.remove();
});

test('a titled sub-control inside the card body keeps its own box (it is another control)', async () => {
	const wrap = build('span', { 'data-custom-tip': '' });
	const card = build('div', { 'data-hover-card': '' }, wrap);
	const row = build('button', { title: 'Two-track AutoPlay - planned' }, card);
	pointer('pointerover', row);
	await Promise.resolve();
	assert.equal(visibleTip()?.textContent, 'Two-track AutoPlay - planned');
	pointer('pointerout', row, doc.body);
	await Promise.resolve();
	wrap.remove();
});

test('card body text under a titled host outside the body still shows no box', async () => {
	const host = build('span', { 'data-custom-tip': '', title: 'host help' });
	const card = build('div', { 'data-hover-card': '' }, host);
	const text = build('p', {}, card);
	pointer('pointerover', text);
	await Promise.resolve();
	assert.equal(visibleTip(), undefined, 'the host title belongs outside the body');
	pointer('pointerout', text, doc.body);
	await Promise.resolve();
	host.remove();
});

/** Every element from `el` up to the document that still has a live `title`. */
function liveTitlesInChain(el) {
	const out = [];
	for (let n = el; n !== null; n = n.parentElement) {
		if (n.hasAttribute('title')) out.push(n);
	}
	return out;
}

test('nested titles: no element in the hovered chain keeps a live title, all restored on leave', async () => {
	// Tue 6 Oct 2026, the maintainer: "double hover-explainers still an issue". The deck's
	// stems chip had a title inside a titled stems row; parking only the chip let
	// the browser fall back to the row's title and draw a native tooltip anyway.
	const panel = build('section', { title: 'Deck 1' });
	const stems = build('div', { title: 'This track has no stems: none on this machine and none in the cloud.' }, panel);
	const plain = build('span', {}, stems);
	const chip = build('button', { title: 'VOCAL: no stem bundle' }, plain);
	pointer('pointerover', chip);
	await Promise.resolve();
	assert.deepEqual(liveTitlesInChain(chip), [], 'no native tooltip can draw from any ancestor');
	assert.equal(visibleTip()?.textContent, 'VOCAL: no stem bundle', 'the hovered chip owns the one tip');
	assert.equal(stems.getAttribute('aria-describedby'), null, 'ancestors are not described by the tip');
	assert.equal(plain.hasAttribute('data-tip-parked'), false, 'an untitled ancestor is left alone');
	pointer('pointerout', chip, doc.body);
	await Promise.resolve();
	assert.equal(chip.getAttribute('title'), 'VOCAL: no stem bundle');
	assert.equal(stems.getAttribute('title'), 'This track has no stems: none on this machine and none in the cloud.');
	assert.equal(panel.getAttribute('title'), 'Deck 1');
	for (const el of [panel, stems, plain, chip]) {
		assert.equal(el.hasAttribute('data-tip-parked'), false);
		assert.equal(el.hasAttribute('data-tip'), false);
	}
	panel.remove();
});

test('nested titles: moving from the chip onto its titled row hands the tip to the row', async () => {
	const stems = build('div', { title: 'Stems row help' });
	const chip = build('button', { title: 'Chip help' }, stems);
	pointer('pointerover', chip);
	await Promise.resolve();
	pointer('pointerout', chip, stems);
	pointer('pointerover', stems);
	await Promise.resolve();
	assert.equal(visibleTip()?.textContent, 'Stems row help');
	assert.equal(chip.getAttribute('title'), 'Chip help', 'the chip is restored once the pointer leaves it');
	assert.equal(stems.hasAttribute('title'), false);
	pointer('pointerout', stems, doc.body);
	await Promise.resolve();
	assert.equal(stems.getAttribute('title'), 'Stems row help');
	stems.remove();
});

test('single title under untitled ancestors: only the host is parked, as before', async () => {
	const outer = build('div');
	const button = build('button', { title: 'Loop length' }, outer);
	pointer('pointerover', button);
	await Promise.resolve();
	assert.equal(button.hasAttribute('title'), false);
	assert.equal(outer.hasAttribute('data-tip-parked'), false);
	assert.equal(outer.hasAttribute('data-tip'), false);
	assert.equal(visibleTip()?.textContent, 'Loop length');
	pointer('pointerout', button, doc.body);
	await Promise.resolve();
	assert.equal(button.getAttribute('title'), 'Loop length');
	outer.remove();
});
