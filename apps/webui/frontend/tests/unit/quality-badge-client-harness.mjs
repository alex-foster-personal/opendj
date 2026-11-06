/**
 * Client-side QualityBadge harness for node:test (real Svelte client + React 19),
 * with a minimal DOM shim. Not a *.test.mjs — imported by audio-quality-hover.test.mjs.
 */
import { readFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from './esbuild-tsconfig.mjs';
import { compile } from 'svelte/compiler';

import { importBundledSource } from './import-bundled-source.mjs';
import { viteUrlSuffixPlugin } from './vite-url-suffix-plugin.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));

const NODE_ELEMENT = 1;
const NODE_TEXT = 3;
const NODE_COMMENT = 8;
const NODE_DOCUMENT = 9;
const NODE_DOCUMENT_FRAGMENT = 11;

/** @type {string[]} */
let _installedKeys = [];
/** @type {{ key: string, desc: PropertyDescriptor | undefined }[]} */
let _restores = [];
/** @type {(() => void) | null} */
let _teardownDom = null;

/**
 * @returns {() => void}
 */
export function installQualityBadgeDom() {
	if (_teardownDom) {
		return _teardownDom;
	}
	const prev = {};
	const take = (key) => {
		prev[key] = globalThis[key];
		_installedKeys.push(key);
	};

	const doc = new DocumentShim();

	take('document');
	take('window');
	take('Node');
	take('Element');
	take('HTMLElement');
	take('Text');
	take('Comment');
	take('Document');
	take('DocumentFragment');
	take('HTMLMediaElement');
	take('HTMLIFrameElement');
	globalThis.document = doc;
	globalThis.window = globalThis;
	// Svelte client `init_operations` reads `Node.prototype` / `Element.prototype`; a plain
	// constants object is not a constructor and breaks `getOwnPropertyDescriptor`.
	NodeShim.ELEMENT_NODE = NODE_ELEMENT;
	NodeShim.TEXT_NODE = NODE_TEXT;
	NodeShim.COMMENT_NODE = NODE_COMMENT;
	NodeShim.DOCUMENT_NODE = NODE_DOCUMENT;
	NodeShim.DOCUMENT_FRAGMENT_NODE = NODE_DOCUMENT_FRAGMENT;
	globalThis.Node = NodeShim;
	globalThis.Document = DocumentShim;
	globalThis.Element = ElementShim;
	globalThis.HTMLElement = ElementShim;
	globalThis.Text = TextShim;
	globalThis.Comment = CommentShim;
	globalThis.DocumentFragment = DocumentFragmentShim;
	globalThis.HTMLMediaElement = ElementShim;
	globalThis.HTMLIFrameElement = class HTMLIFrameElementShim extends ElementShim {
		/** @param {DocumentShim} doc */
		constructor(doc) {
			super('iframe', doc);
		}
	};

	_restores.push({
		key: 'navigator',
		desc: Object.getOwnPropertyDescriptor(globalThis, 'navigator')
	});
	Object.defineProperty(globalThis, 'navigator', {
		configurable: true,
		enumerable: true,
		writable: true,
		value: { userAgent: 'NodeTest' }
	});

	_teardownDom = () => {
		for (const k of _installedKeys) {
			if (prev[k] === undefined) delete globalThis[k];
			else globalThis[k] = prev[k];
		}
		_installedKeys = [];
		for (const { key, desc } of _restores) {
			if (desc === undefined) delete globalThis[key];
			else Object.defineProperty(globalThis, key, desc);
		}
		_restores = [];
		_teardownDom = null;
	};
	return _teardownDom;
}

class NodeShim {
	/** @param {number} nodeType */
	constructor(nodeType, nodeName, ownerDocument) {
		this.nodeType = nodeType;
		this.nodeName = nodeName;
		/** @type {DocumentShim | null} */
		this.ownerDocument = ownerDocument;
		/** @type {NodeShim | null} */
		this.parentNode = null;
		/** @type {ElementShim | null} */
		this.parentElement = null;
		/** @type {NodeShim[]} */
		this.childNodes = [];
		/** @type { { type: string, fn: Function, capture: boolean }[] } */
		this.listeners = [];
		this.isConnected = false;
	}

	_isUnderDocument() {
		let n = /** @type {NodeShim | null} */ (this);
		while (n) {
			if (n instanceof DocumentShim) return true;
			n = n.parentNode;
		}
		return false;
	}

	get firstChild() {
		return this.childNodes[0] ?? null;
	}

	get lastChild() {
		const c = this.childNodes;
		return c.length ? c[c.length - 1] : null;
	}

	get nextSibling() {
		const p = this.parentNode;
		if (!p) return null;
		const i = p.childNodes.indexOf(this);
		return i >= 0 && i < p.childNodes.length - 1 ? p.childNodes[i + 1] : null;
	}

	get previousSibling() {
		const p = this.parentNode;
		if (!p) return null;
		const i = p.childNodes.indexOf(this);
		return i > 0 ? p.childNodes[i - 1] : null;
	}

	_updateParentLinks() {
		let p = this.parentNode;
		let el = null;
		while (p) {
			if (p.nodeType === NODE_ELEMENT) {
				el = /** @type {ElementShim} */ (p);
				break;
			}
			p = p.parentNode;
		}
		this.parentElement = el;
		this.isConnected = this._isUnderDocument();
		for (const c of this.childNodes) c._updateParentLinks();
	}

	/** @param {NodeShim} child */
	appendChild(child) {
		if (child.nodeType === NODE_DOCUMENT_FRAGMENT) {
			const frag = /** @type {DocumentFragmentShim} */ (child);
			while (frag.firstChild) {
				this.appendChild(frag.firstChild);
			}
			return child;
		}
		if (child.parentNode) {
			child.parentNode.removeChild(child);
		}
		this.childNodes.push(child);
		child.parentNode = this;
		child._updateParentLinks();
		return child;
	}

	/** @param {(NodeShim | string)[]} nodes */
	append(...nodes) {
		for (const n of nodes) {
			if (typeof n === 'string') {
				this.appendChild(new TextShim(n, this.ownerDocument));
			} else {
				this.appendChild(n);
			}
		}
	}

	/** @param {NodeShim} newChild @param {NodeShim | null} refChild */
	insertBefore(newChild, refChild) {
		if (newChild.nodeType === NODE_DOCUMENT_FRAGMENT) {
			const frag = /** @type {DocumentFragmentShim} */ (newChild);
			const nodes = [...frag.childNodes];
			let inserted = newChild;
			for (const n of nodes) {
				inserted = this.insertBefore(n, refChild);
			}
			return inserted;
		}
		if (newChild.parentNode) {
			newChild.parentNode.removeChild(newChild);
		}
		if (refChild === null) {
			return this.appendChild(newChild);
		}
		const i = this.childNodes.indexOf(refChild);
		if (i < 0) throw new Error('insertBefore: refChild not found');
		this.childNodes.splice(i, 0, newChild);
		newChild.parentNode = this;
		newChild._updateParentLinks();
		return newChild;
	}

	/** @param {...NodeShim} nodes */
	before(...nodes) {
		const p = this.parentNode;
		if (!p) throw new Error('before: no parent');
		let ref = this;
		for (const n of nodes) {
			p.insertBefore(n, ref);
		}
	}

	/** @param {...NodeShim} nodes */
	after(...nodes) {
		const p = this.parentNode;
		if (!p) throw new Error('after: no parent');
		let ref = this.nextSibling;
		for (const n of nodes) {
			p.insertBefore(n, ref);
		}
	}

	/**
	 * @param {boolean} [deep]
	 * @returns {NodeShim}
	 */
	cloneNode(deep) {
		if (this.nodeType === NODE_TEXT) {
			return new TextShim(/** @type {TextShim} */ (this).nodeValue, this.ownerDocument);
		}
		if (this.nodeType === NODE_COMMENT) {
			return new CommentShim(/** @type {CommentShim} */ (this).nodeValue, this.ownerDocument);
		}
		if (this.nodeType === NODE_DOCUMENT_FRAGMENT) {
			const f = new DocumentFragmentShim(/** @type {DocumentShim} */ (this.ownerDocument));
			if (deep) {
				for (const c of this.childNodes) {
					f.appendChild(/** @type {NodeShim} */ (c.cloneNode(true)));
				}
			}
			return f;
		}
		if (this.nodeType === NODE_ELEMENT) {
			const el = /** @type {ElementShim} */ (this);
			const tag = el.nodeName.toLowerCase();
			const clone =
				tag === 'template'
					? new TemplateElementShim(/** @type {DocumentShim} */ (el.ownerDocument))
					: new ElementShim(tag, el.ownerDocument);
			for (const [k, v] of el.attrs) {
				clone.setAttribute(k, v);
			}
			if (tag === 'template' && deep) {
				const t = /** @type {TemplateElementShim} */ (el);
				const tc = /** @type {TemplateElementShim} */ (clone);
				for (const c of t.content.childNodes) {
					tc.content.appendChild(/** @type {NodeShim} */ (c.cloneNode(true)));
				}
			} else if (deep) {
				for (const c of this.childNodes) {
					clone.appendChild(/** @type {NodeShim} */ (c.cloneNode(true)));
				}
			}
			return clone;
		}
		const o = Object.create(Object.getPrototypeOf(this));
		Object.assign(o, this);
		o.childNodes = deep ? [...this.childNodes] : [];
		return o;
	}

	/** @param {NodeShim} child */
	removeChild(child) {
		const i = this.childNodes.indexOf(child);
		if (i < 0) throw new Error('removeChild: not a child');
		this.childNodes.splice(i, 1);
		child.parentNode = null;
		child.parentElement = null;
		child.isConnected = false;
		return child;
	}

	remove() {
		const p = this.parentNode;
		if (p) p.removeChild(this);
	}

	// eslint-disable-next-line class-methods-use-this -- DOM API shape
	contains(other) {
		let n = other;
		while (n) {
			if (n === this) return true;
			n = n.parentNode;
		}
		return false;
	}

	/**
	 * @param {{ composed?: boolean } | undefined} [opts]
	 * @returns {NodeShim}
	 */
	getRootNode(opts) {
		let n = /** @type {NodeShim} */ (this);
		while (n.parentNode) n = n.parentNode;
		if (opts?.composed && n.ownerDocument) {
			return n.ownerDocument;
		}
		return n;
	}

	addEventListener(type, fn, options) {
		const capture =
			typeof options === 'boolean'
				? options
				: options !== null && options !== undefined && options.capture === true;
		this.listeners.push({ type, fn, capture });
	}

	removeEventListener(type, fn, options) {
		const capture =
			typeof options === 'boolean'
				? options
				: options !== null && options !== undefined && options.capture === true;
		this.listeners = this.listeners.filter(
			(l) => l.type !== type || l.fn !== fn || l.capture !== capture
		);
	}

	dispatchEvent(event) {
		event.target = this;
		/** Target to root (innermost first). */
		const chain = [];
		let n = /** @type {NodeShim | null} */ (this);
		while (n) {
			chain.push(n);
			n = n.parentNode;
		}
		if (this.ownerDocument) chain.push(this.ownerDocument);
		// Capture: root -> target; bubble: target -> root (matches React delegation on createRoot container).
		for (const node of [...chain].reverse()) {
			for (const l of node.listeners) {
				if (l.type === event.type && l.capture) l.fn.call(node, event);
			}
		}
		for (const node of chain) {
			for (const l of node.listeners) {
				if (l.type === event.type && !l.capture) l.fn.call(node, event);
			}
		}
		return true;
	}
}

class DocumentShim extends NodeShim {
	constructor() {
		super(NODE_DOCUMENT, '#document', null);
		this.ownerDocument = /** @type {any} */ (this);
		/** @type {ElementShim | null} */
		this.activeElement = null;
		this.documentElement = new ElementShim('html', this);
		this.head = new ElementShim('head', this);
		this.body = new ElementShim('body', this);
		this.documentElement.appendChild(this.head);
		this.documentElement.appendChild(this.body);
		this.appendChild(this.documentElement);
	}

	/** @returns {typeof globalThis} */
	get defaultView() {
		return globalThis.window;
	}

	/** @param {string} tag */
	createElement(tag) {
		const t = String(tag).toLowerCase();
		if (t === 'template') {
			return new TemplateElementShim(this);
		}
		return new ElementShim(tag, this);
	}

	/** @param {string} text */
	createTextNode(text) {
		return new TextShim(text, this);
	}

	/** @param {string} data */
	createComment(data) {
		return new CommentShim(data, this);
	}

	createDocumentFragment() {
		return new DocumentFragmentShim(this);
	}

	/** @param {NodeShim} node @param {boolean} deep */
	importNode(node, deep) {
		return /** @type {NodeShim} */ (node.cloneNode(deep));
	}

	querySelector(sel) {
		return this.body.querySelector(sel);
	}

	querySelectorAll(sel) {
		return this.body.querySelectorAll(sel);
	}
}

class TextShim extends NodeShim {
	/** @param {string} text */
	constructor(text, ownerDocument) {
		super(NODE_TEXT, '#text', ownerDocument);
		this.nodeValue = String(text);
	}

	get textContent() {
		return this.nodeValue;
	}

	set textContent(v) {
		this.nodeValue = String(v ?? '');
	}
}

class CommentShim extends NodeShim {
	constructor(data, ownerDocument) {
		super(NODE_COMMENT, '#comment', ownerDocument);
		this.nodeValue = String(data);
	}
}

class DocumentFragmentShim extends NodeShim {
	/** @param {DocumentShim} ownerDocument */
	constructor(ownerDocument) {
		super(NODE_DOCUMENT_FRAGMENT, '#document-fragment', ownerDocument);
	}

	get innerHTML() {
		return this.childNodes
			.map((c) => {
				if (c.nodeType === NODE_TEXT) return /** @type {TextShim} */ (c).nodeValue;
				if (c.nodeType === NODE_COMMENT)
					return `<!--${/** @type {CommentShim} */ (c).nodeValue}-->`;
				if (c.nodeType === NODE_ELEMENT) return /** @type {ElementShim} */ (c).innerHTML;
				return '';
			})
			.join('');
	}

	set innerHTML(html) {
		while (this.firstChild) {
			this.removeChild(this.firstChild);
		}
		parseHtmlIntoParent(this, String(html), 0);
	}

	querySelector(selector) {
		const walk = (node) => {
			if (node.nodeType === NODE_ELEMENT && /** @type {ElementShim} */ (node).matches(selector)) {
				return /** @type {ElementShim} */ (node);
			}
			for (const c of node.childNodes) {
				const f = walk(c);
				if (f) return f;
			}
			return null;
		};
		for (const c of this.childNodes) {
			const f = walk(c);
			if (f) return f;
		}
		return null;
	}

	querySelectorAll(selector) {
		const out = [];
		const walk = (node) => {
			if (node.nodeType === NODE_ELEMENT && /** @type {ElementShim} */ (node).matches(selector)) {
				out.push(node);
			}
			for (const c of node.childNodes) walk(c);
		};
		for (const c of this.childNodes) walk(c);
		return out;
	}
}

const VOID_HTML = new Set([
	'area',
	'base',
	'br',
	'col',
	'embed',
	'hr',
	'img',
	'input',
	'link',
	'meta',
	'param',
	'source',
	'track',
	'wbr'
]);

/** @param {ElementShim} el @param {string} attrStr */
function _parseOpeningAttributes(el, attrStr) {
	const re = /([\w:-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
	let m;
	while ((m = re.exec(attrStr))) {
		const name = m[1];
		const value = m[2] ?? m[3] ?? m[4] ?? '';
		el.setAttribute(name, value);
	}
}

/**
 * Parse minimal HTML into parent. Stops when `</stopTag>` is encountered (if stopTag set).
 * @param {ElementShim | DocumentFragmentShim} parent
 * @param {string} html
 * @param {number} start
 * @param {string | null} [stopTag]
 * @returns {number} next index
 */
function parseHtmlIntoParent(parent, html, start, stopTag = null) {
	const doc = parent.ownerDocument;
	let i = start;
	const len = html.length;

	while (i < len) {
		if (
			stopTag !== null &&
			html.slice(i, i + 2 + stopTag.length + 1).toLowerCase() === `</${stopTag.toLowerCase()}>`
		) {
			const gt = html.indexOf('>', i);
			return gt < 0 ? len : gt + 1;
		}

		if (html[i] !== '<') {
			const nextLt = html.indexOf('<', i);
			const chunk = nextLt === -1 ? html.slice(i) : html.slice(i, nextLt);
			if (chunk) parent.appendChild(new TextShim(chunk, doc));
			i = nextLt === -1 ? len : nextLt;
			continue;
		}

		if (html.startsWith('<!--', i)) {
			const end = html.indexOf('-->', i + 4);
			const data = end === -1 ? html.slice(i + 4) : html.slice(i + 4, end);
			parent.appendChild(new CommentShim(data, doc));
			i = end === -1 ? len : end + 3;
			continue;
		}

		if (html[i + 1] === '/') {
			const gt = html.indexOf('>', i);
			i = gt < 0 ? len : gt + 1;
			continue;
		}

		const gt = html.indexOf('>', i);
		if (gt < 0) break;

		let rawTag = html.slice(i + 1, gt).trim();
		const selfClosing = rawTag.endsWith('/');
		if (selfClosing) rawTag = rawTag.slice(0, -1).trim();

		const nameMatch = rawTag.match(/^([\w:-]+)/);
		const tagName = nameMatch ? nameMatch[1].toLowerCase() : 'div';
		const attrStr = rawTag.slice(tagName.length).trim();

		/** @type {ElementShim | TemplateElementShim} */
		let el;
		if (tagName === 'template') {
			el = new TemplateElementShim(doc);
		} else {
			el = new ElementShim(tagName, doc);
		}
		_parseOpeningAttributes(el, attrStr);
		parent.appendChild(el);

		i = gt + 1;
		if (selfClosing || VOID_HTML.has(tagName)) continue;

		if (tagName === 'template') {
			i = parseHtmlIntoParent(/** @type {TemplateElementShim} */ (el).content, html, i, 'template');
			continue;
		}

		i = parseHtmlIntoParent(el, html, i, tagName);
	}
	return i;
}

class ElementShim extends NodeShim {
	/** @param {string} tag */
	constructor(tag, ownerDocument) {
		super(NODE_ELEMENT, String(tag).toUpperCase(), ownerDocument);
		/** Same as nodeName; React reads tagName during DOM reconciling. */
		Object.defineProperty(this, 'tagName', { get: () => this.nodeName, enumerable: true });
		/** @type {Map<string, string>} */
		this.attrs = new Map();
		this._classTokenSet = new Set();
		this.hidden = false;
		this.disabled = false;
		/** @type {Record<string, string>} */
		this.style = {};
	}

	get innerHTML() {
		return this.childNodes
			.map((c) => {
				if (c.nodeType === NODE_TEXT) return /** @type {TextShim} */ (c).nodeValue;
				if (c.nodeType === NODE_COMMENT)
					return `<!--${/** @type {CommentShim} */ (c).nodeValue}-->`;
				if (c.nodeType === NODE_ELEMENT) {
					const el = /** @type {ElementShim} */ (c);
					const attrs = [...el.attrs.entries()]
						.map(([k, v]) => ` ${k}="${String(v).replace(/"/g, '&quot;')}"`)
						.join('');
					if (el.childNodes.length === 0)
						return `<${el.nodeName.toLowerCase()}${attrs}></${el.nodeName.toLowerCase()}>`;
					return `<${el.nodeName.toLowerCase()}${attrs}>${el.innerHTML}</${el.nodeName.toLowerCase()}>`;
				}
				return '';
			})
			.join('');
	}

	set innerHTML(html) {
		while (this.firstChild) {
			this.removeChild(this.firstChild);
		}
		parseHtmlIntoParent(this, String(html), 0);
	}

	get className() {
		return [...this._classTokenSet].join(' ');
	}

	set className(v) {
		this._classTokenSet = new Set(String(v).split(/\s+/).filter(Boolean));
		this.attrs.set('class', this.className);
	}

	get classList() {
		const el = this;
		return {
			add: (...tokens) => {
				for (const t of tokens) el._classTokenSet.add(t);
				el.attrs.set('class', el.className);
			},
			remove: (...tokens) => {
				for (const t of tokens) el._classTokenSet.delete(t);
				el.attrs.set('class', el.className);
			},
			contains: (t) => el._classTokenSet.has(t),
			toggle: (t, force) => {
				const on = force === undefined ? !el._classTokenSet.has(t) : force;
				if (on) el._classTokenSet.add(t);
				else el._classTokenSet.delete(t);
				el.attrs.set('class', el.className);
				return on;
			}
		};
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
					el.setAttribute(attr, String(value));
					return true;
				}
			}
		);
	}

	setAttribute(name, value) {
		const n = String(name);
		this.attrs.set(n, String(value));
		if (n === 'class') {
			this._classTokenSet = new Set(String(value).split(/\s+/).filter(Boolean));
		}
	}

	getAttribute(name) {
		return this.attrs.has(String(name)) ? this.attrs.get(String(name)) : null;
	}

	hasAttribute(name) {
		return this.attrs.has(String(name));
	}

	removeAttribute(name) {
		const n = String(name);
		if (!this.attrs.has(n)) return;
		this.attrs.delete(n);
		if (n === 'class') this._classTokenSet.clear();
	}

	/** @param {string} selector */
	matches(selector) {
		const s = selector.trim();
		if (s.startsWith('.')) {
			const cls = s.slice(1);
			return cls.split('.').every((c) => this.classList.contains(c));
		}
		return this.tagName.toLowerCase() === s.toLowerCase();
	}

	/** @param {string} selector */
	querySelector(selector) {
		const walk = (node) => {
			if (node.nodeType === NODE_ELEMENT && /** @type {ElementShim} */ (node).matches(selector)) {
				return /** @type {ElementShim} */ (node);
			}
			for (const c of node.childNodes) {
				const f = walk(c);
				if (f) return f;
			}
			return null;
		};
		for (const c of this.childNodes) {
			const f = walk(c);
			if (f) return f;
		}
		return null;
	}

	/** @param {string} selector */
	querySelectorAll(selector) {
		const out = [];
		const walk = (node) => {
			if (node.nodeType === NODE_ELEMENT && /** @type {ElementShim} */ (node).matches(selector)) {
				out.push(node);
			}
			for (const c of node.childNodes) walk(c);
		};
		for (const c of this.childNodes) walk(c);
		return out;
	}

	get textContent() {
		if (this.firstChild === null) return '';
		return this.childNodes
			.map((c) =>
				c.nodeType === NODE_TEXT ? c.nodeValue : /** @type {ElementShim} */ (c).textContent
			)
			.join('');
	}

	set textContent(v) {
		this.childNodes = [];
		this.appendChild(new TextShim(String(v ?? ''), this.ownerDocument));
	}
}

class TemplateElementShim extends ElementShim {
	/** @param {DocumentShim} ownerDocument */
	constructor(ownerDocument) {
		super('template', ownerDocument);
		this.content = new DocumentFragmentShim(ownerDocument);
	}

	get innerHTML() {
		return this.content.innerHTML;
	}

	set innerHTML(html) {
		while (this.content.firstChild) {
			this.content.removeChild(this.content.firstChild);
		}
		parseHtmlIntoParent(this.content, String(html), 0);
	}
}

const SVELTE_HARNESS_SOURCE = `<script>
	import QualityBadge from '$lib/components/rb/QualityBadge.svelte';
	let { quality, ctrl } = $props();
	let trackKey = $state(ctrl.initialTrackKey);
	let loadHover = $state(ctrl.initialLoadHover);
	ctrl.update = (patch) => {
		if (Object.hasOwn(patch, 'trackKey')) trackKey = patch.trackKey;
		if (Object.hasOwn(patch, 'loadHover')) loadHover = patch.loadHover;
	};
</script>
<QualityBadge {quality} {trackKey} compact={true} showContainer={false} {loadHover} />
`;

const svelteClientPlugin = {
	name: 'quality-badge-svelte-client',
	setup(b) {
		b.onResolve({ filter: /^virtual:quality-badge-harness\.svelte$/ }, () => ({
			path: 'quality-badge-harness.svelte',
			namespace: 'qb-harness'
		}));
		b.onLoad({ filter: /.*/, namespace: 'qb-harness' }, () => ({
			contents: compile(SVELTE_HARNESS_SOURCE, {
				filename: 'QualityBadgeHarness.svelte',
				generate: 'client',
				css: 'injected'
			}).js.code,
			loader: 'js',
			resolveDir: FRONTEND_ROOT
		}));
		b.onLoad({ filter: /\.svelte$/ }, (args) => {
			const source = readFileSync(args.path, 'utf8');
			const out = compile(source, { filename: args.path, generate: 'client', css: 'injected' });
			return { contents: out.js.code, loader: 'js', resolveDir: dirname(args.path) };
		});
	}
};

/**
 * Bundle and import the Svelte harness + real QualityBadge.svelte.
 * Call `installQualityBadgeDom()` before any `svelte` import elsewhere; this function does not import svelte at module scope.
 */
export async function loadSvelteBadgeHarness() {
	installQualityBadgeDom();
	const entry = `
import { mount, unmount, flushSync } from 'svelte';
import Harness from 'virtual:quality-badge-harness.svelte';
export function mountSvelteQualityBadge(target, ctrl) {
	const instance = mount(Harness, { target, props: { quality: ctrl.quality, ctrl } });
	return {
		flush: () => { flushSync(() => {}); },
		unmount: () => { unmount(instance); },
		instance
	};
}
`;
	const result = await build({
		stdin: {
			contents: entry,
			resolveDir: FRONTEND_ROOT,
			loader: 'js',
			sourcefile: 'quality-badge-svelte-entry.mjs'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		define: { 'import.meta.env': '{}' },
		format: 'esm',
		logLevel: 'silent',
		platform: 'browser',
		plugins: [viteUrlSuffixPlugin, svelteClientPlugin],
		target: 'es2022',
		write: false
	});
	if (!result.outputFiles[0]) throw new Error('svelte badge harness: empty bundle');
	return importBundledSource(result.outputFiles[0].text, 'quality-badge-svelte-harness');
}

/**
 * Bundle and import the real QualityBadge.tsx (CSS emptied) plus React and react-dom/client for one runtime instance.
 */
export async function loadReactBadge() {
	installQualityBadgeDom();
	const result = await build({
		stdin: {
			contents: `import QualityBadge from './src/lib/components/rb/QualityBadge.tsx';
import * as React from 'react';
import { createRoot } from 'react-dom/client';
import { flushSync } from 'react-dom';
export { QualityBadge, React, createRoot, flushSync };
`,
			resolveDir: FRONTEND_ROOT,
			loader: 'tsx',
			sourcefile: 'quality-badge-react-entry.tsx'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		define: { 'import.meta.env': '{}' },
		format: 'esm',
		loader: { '.css': 'empty', '.tsx': 'tsx', '.ts': 'ts' },
		logLevel: 'silent',
		platform: 'browser',
		target: 'es2022',
		write: false,
		plugins: [viteUrlSuffixPlugin]
	});
	if (!result.outputFiles[0]) throw new Error('react QualityBadge: empty bundle');
	return importBundledSource(result.outputFiles[0].text, 'quality-badge-react');
}

/**
 * @param {import('react').ComponentType<any>} QualityBadge
 * @param {import('react')} React
 * @param {typeof import('react-dom/client').createRoot} createRoot
 * @param {typeof import('react-dom').flushSync} flushSync
 */
export function createReactBadgeController(QualityBadge, React, createRoot, flushSync) {
	const { createElement } = React;
	return {
		/**
		 * @param {ElementShim} container
		 * @param {{ quality: unknown, trackKey?: string, loadHover?: () => Promise<string> }} initial
		 */
		mount(container, initial) {
			const root = createRoot(container);
			const state = { ...initial };
			const render = () => {
				root.render(
					createElement(QualityBadge, {
						quality: state.quality,
						trackKey: state.trackKey ?? '',
						compact: true,
						showContainer: false,
						loadHover: state.loadHover
					})
				);
			};
			flushSync(render);
			return {
				flush: () => {
					flushSync(render);
				},
				update(patch) {
					Object.assign(state, patch);
					flushSync(render);
				},
				unmount: () => root.unmount()
			};
		}
	};
}
