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
		this.classList = {
			add: () => {},
			remove: () => {},
			toggle: () => {},
			contains: () => false
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
