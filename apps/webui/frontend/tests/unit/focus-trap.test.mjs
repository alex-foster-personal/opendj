/**
 * Issue #10364: shared overlay focusTrap and nextTabStop.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, after, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let realDocument;

const cand = (overrides = {}) => ({
	disabled: false,
	tabIndex: 0,
	ariaHidden: false,
	rendered: true,
	...overrides
});

before(async () => {
	mod = await loadTypeScriptModule('src/lib/keyboard/focus-trap.ts');
});

test('isTabStop truth table', () => {
	assert.equal(mod.isTabStop(cand()), true);
	assert.equal(mod.isTabStop(cand({ disabled: true })), false);
	assert.equal(mod.isTabStop(cand({ tabIndex: -1 })), false);
	assert.equal(mod.isTabStop(cand({ ariaHidden: true })), false);
	assert.equal(mod.isTabStop(cand({ rendered: false })), false);
});

test('nextTabStop synthetic list wrap and skip', () => {
	const a = cand({ label: 'a' });
	const b = cand({ disabled: true, label: 'b' });
	const c = cand({ tabIndex: -1, label: 'c' });
	const d = cand({ ariaHidden: true, label: 'd' });
	const e = cand({ label: 'e' });
	const f = cand({ rendered: false, label: 'f' });
	const list = [a, b, c, d, e, f];

	assert.equal(mod.nextTabStop(list, e, false), a);
	assert.equal(mod.nextTabStop(list, a, true), e);
	assert.equal(mod.nextTabStop(list, a, false), e);
	assert.equal(mod.nextTabStop(list, e, true), a);
	assert.equal(mod.nextTabStop(list, null, false), a);
	assert.equal(mod.nextTabStop(list, null, true), e);
	assert.equal(mod.nextTabStop(list, c, false), e);
	assert.equal(mod.nextTabStop(list, c, true), a);

	const solo = [cand({ label: 'only' })];
	assert.equal(mod.nextTabStop(solo, solo[0], false), solo[0]);
	assert.equal(mod.nextTabStop([b, d, f], null, false), null);
});

function fakeEl({
	disabled = false,
	tabIndex = 0,
	ariaHidden = false,
	rendered = true,
	label = 'el'
} = {}) {
	return {
		label,
		tabIndex,
		matches: (sel) => (sel === ':disabled' ? disabled : false),
		closest: (sel) => (ariaHidden && sel.includes('aria-hidden') ? {} : null),
		checkVisibility: rendered ? () => true : () => false,
		getClientRects: () => (rendered ? [{ width: 1 }] : []),
		focus() {
			if (fakeDocument) fakeDocument.activeElement = this;
		},
		isConnected: true
	};
}

let fakeDocument;

test('collectTabStops maps flags and getClientRects fallback', () => {
	const withVis = fakeEl();
	const noVis = {
		...fakeEl({ rendered: false }),
		checkVisibility: undefined
	};
	const root = {
		querySelectorAll: () => [withVis, noVis]
	};
	const stops = mod.collectTabStops(root);
	assert.equal(stops.length, 2);
	assert.equal(stops[0].disabled, false);
	assert.equal(stops[0].rendered, true);
	assert.equal(stops[1].rendered, false);
});

test('focusTrap action with fake document', async () => {
	realDocument = globalThis.document;
	const opener = fakeEl({ label: 'opener' });
	opener.isConnected = true;
	const body = { focus() {} };
	const listeners = [];
	fakeDocument = {
		activeElement: opener,
		body,
		addEventListener(type, fn) {
			if (type === 'keydown') listeners.push(fn);
		},
		removeEventListener(type, fn) {
			const i = listeners.indexOf(fn);
			if (i !== -1) listeners.splice(i, 1);
		}
	};
	globalThis.document = fakeDocument;

	const inner = fakeEl({ label: 'search' });
	const close = fakeEl({ label: 'close' });
	const node = {
		label: 'dialog',
		tabIndex: -1,
		isConnected: true,
		contains(el) {
			return el === node || el === inner || el === close;
		},
		querySelector(sel) {
			return sel === '.search' ? inner : null;
		},
		querySelectorAll: () => [inner, close],
		focus() {
			fakeDocument.activeElement = this;
		}
	};

	const trap = mod.focusTrap(node, { initialFocus: '.search' });
	assert.equal(listeners.length, 1);

	await Promise.resolve();
	assert.equal(fakeDocument.activeElement, inner);

	fakeDocument.activeElement = close;
	const tabEv = {
		key: 'Tab',
		shiftKey: false,
		altKey: false,
		ctrlKey: false,
		metaKey: false,
		defaultPrevented: false,
		preventDefault() {
			this.defaultPrevented = true;
		}
	};
	listeners[0](tabEv);
	assert.equal(tabEv.defaultPrevented, true);
	assert.equal(fakeDocument.activeElement, inner);

	fakeDocument.activeElement = body;
	const nodeAlt = {
		...node,
		label: 'alt',
		querySelector: () => null,
		querySelectorAll: () => [inner, close],
		contains(el) {
			return el === nodeAlt;
		}
	};
	const trapSelf = mod.focusTrap(nodeAlt, { initialFocus: 'self' });
	await Promise.resolve();
	assert.equal(fakeDocument.activeElement, nodeAlt);

	const nodeSkip = {
		...nodeAlt,
		label: 'skip',
		contains(el) {
			return el === nodeSkip || el === inner || el === close;
		}
	};
	const trapSkip = mod.focusTrap(nodeSkip, {});
	fakeDocument.activeElement = inner;
	await Promise.resolve();
	assert.equal(fakeDocument.activeElement, inner);

	const foreignDialog = {
		...fakeEl(),
		getAttribute: (n) => (n === 'role' ? 'dialog' : null),
		closest(sel) {
			if (String(sel).includes('dialog')) return this;
			return null;
		}
	};
	fakeDocument.activeElement = foreignDialog;
	const tabForeign = {
		...tabEv,
		defaultPrevented: false,
		preventDefault() {
			this.defaultPrevented = true;
		}
	};
	listeners[0](tabForeign);
	assert.equal(tabForeign.defaultPrevented, false);

	trapSelf.destroy();
	trapSkip.destroy();
	fakeDocument.activeElement = body;
	trap.destroy();

	assert.equal(fakeDocument.activeElement, opener);
	assert.equal(listeners.length, 0);
});

after(() => {
	if (realDocument) globalThis.document = realDocument;
});

test('stacked traps: newest handles Tab until destroyed', async () => {
	realDocument = globalThis.document;
	const listeners = [];
	fakeDocument = {
		activeElement: null,
		body: {},
		addEventListener(type, fn) {
			if (type === 'keydown') listeners.push(fn);
		},
		removeEventListener() {}
	};
	globalThis.document = fakeDocument;

	const a = fakeEl({ label: 'a' });
	const nodeA = {
		isConnected: true,
		contains: (el) => el === a,
		querySelectorAll: () => [a],
		focus() {},
		querySelector: () => null,
		tabIndex: -1
	};
	const nodeB = { ...nodeA, label: 'b' };
	const tA = mod.focusTrap(nodeA, {});
	const tB = mod.focusTrap(nodeB, {});
	assert.equal(listeners.length, 2);

	fakeDocument.activeElement = a;
	const ev = {
		key: 'Tab',
		shiftKey: false,
		altKey: false,
		ctrlKey: false,
		metaKey: false,
		defaultPrevented: false,
		preventDefault() {
			this.defaultPrevented = true;
		}
	};
	listeners[1](ev);
	assert.equal(ev.defaultPrevented, true);

	tB.destroy();
	listeners[0](ev);
	assert.equal(ev.defaultPrevented, true);
	tA.destroy();
	globalThis.document = realDocument;
});

//----- KEYB (#12271): a removed opener hands focus to its nearest surviving control
// A fake tree with the slice destroy reads: isConnected, parentElement, focus,
// closest('[aria-modal="true"]'), contains and querySelectorAll over controls.

function treeDocument() {
	const doc = {
		activeElement: null,
		body: null,
		addEventListener() {},
		removeEventListener() {}
	};
	const root = treeNode(null, { control: false });
	function treeNode(parent, { control = true, modal = false, label = 'node' } = {}) {
		const n = {
			label,
			parentElement: parent,
			children: [],
			control,
			modal,
			tabIndex: control ? 0 : -1,
			get isConnected() {
				return n === root || (n.parentElement !== null && n.parentElement.isConnected);
			},
			focus() {
				if ((n.control || n.modal) && n.isConnected) doc.activeElement = n;
			},
			blur() {
				if (doc.activeElement === n) doc.activeElement = doc.body;
			},
			closest(selector) {
				assert.equal(selector, '[aria-modal="true"]');
				for (let at = n; at !== null; at = at.parentElement) if (at.modal) return at;
				return null;
			},
			contains(el) {
				for (let at = el; at !== null && at !== undefined; at = at.parentElement)
					if (at === n) return true;
				return false;
			},
			querySelector: () => null,
			querySelectorAll() {
				const out = [];
				const walk = (c) => c.children.forEach((k) => (k.control && out.push(k), walk(k)));
				walk(n);
				return out;
			},
			remove() {
				n.parentElement.children.splice(n.parentElement.children.indexOf(n), 1);
				n.parentElement = null;
			}
		};
		parent?.children.push(n);
		return n;
	}
	const body = treeNode(root, { control: false, label: 'body' });
	doc.body = body;
	const header = treeNode(body, { control: false });
	const headerButton = treeNode(header, { label: 'headerButton' });
	const main = treeNode(body, { control: false });
	const list = treeNode(main, { control: false, label: 'list' });
	const opener = treeNode(list, { label: 'opener' });
	const sibling = treeNode(list, { label: 'sibling' });
	opener.focus();
	const dialog = treeNode(body, { control: false, modal: true, label: 'dialog' });
	const search = treeNode(dialog, { label: 'search' });
	return { doc, body, headerButton, list, opener, sibling, dialog, search };
}

async function withTreeDocument(run) {
	const saved = globalThis.document;
	const tree = treeDocument();
	globalThis.document = tree.doc;
	try {
		await run(tree);
	} finally {
		if (saved === undefined) delete globalThis.document;
		else globalThis.document = saved;
	}
}

test('if the opener is still connected at destroy [then] focus returns to it, [else stop].', async () => {
	await withTreeDocument(async (t) => {
		const trap = mod.focusTrap(t.dialog, { initialFocus: 'self' });
		await Promise.resolve();
		t.search.focus();
		trap.destroy();
		assert.equal(t.doc.activeElement, t.opener);
	});
});

test('if the opener was removed before destroy [then] focus goes to the first control in its nearest surviving ancestor, not body, [else stop].', async () => {
	await withTreeDocument(async (t) => {
		const trap = mod.focusTrap(t.dialog, { initialFocus: 'self' });
		await Promise.resolve();
		t.search.focus();
		t.opener.remove();
		assert.doesNotThrow(() => trap.destroy());
		assert.equal(t.doc.activeElement, t.sibling, 'the opener list keeps focus');
	});
	await withTreeDocument(async (t) => {
		const trap = mod.focusTrap(t.dialog, { initialFocus: 'self' });
		await Promise.resolve();
		t.search.focus();
		t.list.remove();
		trap.destroy();
		assert.notEqual(t.doc.activeElement, t.body, 'focus fell to the page body');
		assert.equal(t.doc.activeElement, t.headerButton, 'next page control, not the modal');
	});
});

test('if nothing outside a modal takes focus after the opener is removed [then] destroy blurs as before, [else stop].', async () => {
	await withTreeDocument(async (t) => {
		const trap = mod.focusTrap(t.dialog, { initialFocus: 'self' });
		await Promise.resolve();
		t.search.focus();
		t.headerButton.remove();
		t.list.remove();
		trap.destroy();
		assert.equal(t.doc.activeElement, t.body, 'only the closing modal had controls left');
	});
});

test('source contract: four overlays use focusTrap', () => {
	const files = [
		'src/lib/components/settings/SettingsOverlay.svelte',
		'src/lib/components/rb/hotkeys/HotkeysOverlayView.svelte',
		'src/lib/components/rb/TrackInfoOverlay.svelte',
		'src/lib/components/setup/SetupOverlay.svelte'
	];
	for (const f of files) {
		const src = readFileSync(f, 'utf8');
		assert.match(src, /from '\$lib\/keyboard\/focus-trap'/);
		assert.match(src, /use:focusTrap/);
	}
	const setup = readFileSync('src/lib/components/setup/SetupOverlay.svelte', 'utf8');
	assert.match(setup, /onkeydown=\{onPanelKeydown\}/);
	assert.match(setup, /\.step-link:focus-visible/);
	const track = readFileSync('src/lib/components/rb/TrackInfoOverlay.svelte', 'utf8');
	assert.doesNotMatch(track, /panelEl\?\.focus\(\)/);
	const hk = readFileSync('src/lib/components/rb/hotkeys/HotkeysOverlayView.svelte', 'utf8');
	const searchIdx = hk.indexOf('class="hk-search"');
	const tabsIdx = hk.indexOf('class="hk-tabs"');
	const closeIdx = hk.indexOf('class="hk-close"');
	assert.ok(searchIdx > 0 && searchIdx < tabsIdx && tabsIdx < closeIdx);
});

test('agent parity: palette opens same overlays', () => {
	const cmds = readFileSync('src/lib/palette/commands.ts', 'utf8');
	assert.match(cmds, /settings\.open.*open_settings/s);
	assert.match(cmds, /hotkeys\.show.*show_hotkeys/s);
});
