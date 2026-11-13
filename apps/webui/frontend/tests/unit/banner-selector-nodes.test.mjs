/**
 * requirement: KEYB-14590 (#14559, epic #9372): the /performance banners are selector groups with one leaf per button.
 *
 * The real ReloadResumeBanner, AutoPlayStallBanner, ReplayTakeoverBar and TabLeaderBanner are
 * client-compiled and mounted on the node DOM shim, so their real `selectorNode` actions run, and
 * the real `snapshotSelectorNodes` reads the registry the page would see (parent = nearest node
 * ancestor, so a banner with none becomes a root).
 *
 * Regression lines:
 * - if a shown banner is not one root group holding exactly one leaf per visible button then broken
 * - if a hidden banner leaves any selector node registered then broken
 * - if clicking the control inside a banner leaf runs anything but that leaf's button then broken
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { bundlePerformanceBannersClient } from './mount-svelte-client.mjs';
import { installQualityBadgeDom } from './quality-badge-client-harness.mjs';

let client;
let teardownDom;

before(async () => {
	teardownDom = installQualityBadgeDom();
	const proto = globalThis.Element.prototype;
	// Shim gaps the selectorNode action and snapshotSelectorNodes need.
	proto.toggleAttribute = function (name, force) {
		if (force) this.setAttribute(name, '');
		else this.removeAttribute(name);
		return force;
	};
	proto.closest = function (selector) {
		const attr = /^\[([\w-]+)\]$/.exec(selector);
		if (attr === null) throw new Error(`banner test closest(): unsupported selector ${selector}`);
		for (let n = this; n !== null; n = n.parentElement) if (n.hasAttribute(attr[1])) return n;
		return null;
	};
	proto.getBoundingClientRect = () => ({ x: 0, y: 0, width: 10, height: 10 });
	globalThis.getComputedStyle = () => ({ display: 'block' });
	globalThis.addEventListener ??= () => {};
	globalThis.removeEventListener ??= () => {};
	const store = new Map();
	Object.defineProperty(globalThis, 'localStorage', {
		configurable: true,
		value: {
			getItem: (k) => store.get(k) ?? null,
			setItem: (k, v) => void store.set(k, String(v)),
			removeItem: (k) => void store.delete(k)
		}
	});
	client = await bundlePerformanceBannersClient();
});

after(() => {
	const proto = globalThis.Element.prototype;
	delete proto.toggleAttribute;
	delete proto.closest;
	delete proto.getBoundingClientRect;
	delete globalThis.getComputedStyle;
	teardownDom?.();
});

//----- helpers

/** Every element under `root` (itself included) in DOM order. */
function walk(root, out = []) {
	if (root.nodeType === 1) out.push(root);
	for (const child of root.childNodes ?? []) walk(child, out);
	return out;
}

/** Enabled buttons under `el`: the only controls these banners carry. */
const buttonsIn = (el) =>
	walk(el).filter((n) => n.nodeName.toLowerCase() === 'button' && !n.hasAttribute('disabled'));

/** The registry as the selector sees it: [id, kind, parentId] rows. */
const registered = () => client.snapshotSelectorNodes().map((n) => [n.id, n.kind, n.parentId]);

/** Click like a pointer would, through Svelte's delegated root listener. */
function click(button) {
	const path = [];
	for (let n = button; n !== null; n = n.parentNode) path.push(n);
	button.dispatchEvent({
		type: 'click',
		bubbles: true,
		timeStamp: 1,
		metaKey: false,
		ctrlKey: false,
		composedPath: () => path
	});
	client.flushSync();
}

/** Mount `Component` straight under document.body (no selector ancestor, like the page). */
function mountBanner(t, Component, props = {}) {
	const host = document.createElement('div');
	document.body.appendChild(host);
	const instance = client.mount(Component, { target: host, props });
	client.flushSync();
	t.after(() => {
		client.unmount(instance);
		host.remove();
	});
	return host;
}

/** The leaf element registered under `id`, found in the mounted markup. */
const leafEl = (host, id) => walk(host).find((n) => n.getAttribute('data-sel-node') === id);

/** Show one banner in its fullest state; returns its mount host. */
function showBanner(t, name) {
	switch (name) {
		case 'reload-resume':
			client.offerReloadResume({ decks: [1, 2], captured_at_ms: 0 }, false);
			t.after(() => client.dismissReloadResume());
			return mountBanner(t, client.ReloadResumeBanner);
		case 'autoplay-stall':
			client.raiseAutoPlayStall(stallWith([{ stable_id: 'a', title: 'A', artist: null }]));
			t.after(() => {
				client.clearAutoPlayStall();
				client.flushSync();
			});
			return mountBanner(t, client.AutoPlayStallBanner);
		case 'replay-takeover':
			return mountBanner(t, client.ReplayTakeoverBar, {
				live: [],
				takenOverAll: false,
				onTakeOverAll: () => {}
			});
		case 'tab-leader':
			client.tabLeadershipView.current = {
				role: 'follower',
				reason: 'another-tab',
				leaseHolder: null
			};
			t.after(() => {
				client.tabLeadershipView.current = { role: 'pending', reason: null, leaseHolder: null };
			});
			return mountBanner(t, client.TabLeaderBanner);
		default:
			throw new Error(`banner test: unknown banner ${name}`);
	}
}

/** A missing-audio stall descriptor carrying `blocked` tracks. */
function stallWith(blocked) {
	return {
		reason: 'missing-audio',
		source_stable_id: 'src',
		headline: 'AutoPlay stopped',
		resume: 'Relink the files',
		detail: null,
		blocked,
		blocked_total: blocked.length
	};
}

/** Group plus leaf rows a shown banner `name` must register, as roots of the selector. */
function expectedRows(name, actions) {
	const group = client.ids.bannerSelId(name);
	return [
		[group, 'group', null],
		...actions.map((a) => [client.ids.bannerLeafSelId(name, a), 'leaf', group])
	];
}

//----- tests

test('KEYB-14590 shown: each banner is one root group with exactly one leaf per visible button', async (t) => {
	// [if] a banner is shown [then] it registers one group with exactly one leaf per visible button, [else stop].
	for (const [name, actions] of Object.entries(client.ids.BANNER_SEL_ACTIONS)) {
		await t.test(name, (tt) => {
			const host = showBanner(tt, name);
			assert.deepEqual(registered(), expectedRows(name, actions), `${name} registry`);
			const leaves = actions.map((a) => leafEl(host, client.ids.bannerLeafSelId(name, a)));
			for (const leaf of leaves)
				assert.equal(buttonsIn(leaf).length, 1, `${name} leaf wraps one button`);
			assert.equal(
				buttonsIn(host).length,
				leaves.length,
				`${name}: a button sits outside every leaf`
			);
		});
	}
});

test('KEYB-14590 shown: a banner showing fewer buttons registers fewer leaves', async (t) => {
	// [if] a shown banner hides one of its buttons [then] that button's leaf is not registered, [else stop].
	await t.test('autoplay-stall with no blocked tracks has no tracks leaf', (tt) => {
		client.raiseAutoPlayStall(stallWith([]));
		tt.after(() => {
			client.clearAutoPlayStall();
			client.flushSync();
		});
		mountBanner(tt, client.AutoPlayStallBanner);
		assert.deepEqual(registered(), expectedRows('autoplay-stall', ['dismiss']));
	});
	await t.test('replay-takeover with every control taken over has no button leaf', (tt) => {
		mountBanner(tt, client.ReplayTakeoverBar, {
			live: [],
			takenOverAll: true,
			onTakeOverAll: () => {}
		});
		assert.deepEqual(registered(), expectedRows('replay-takeover', []));
	});
});

test('KEYB-14590 hidden: a banner that is not shown registers no selector node', async (t) => {
	// [if] a banner is not shown [then] it registers no selector node, [else stop].
	await t.test('idle stores mount banners with nothing on screen', (tt) => {
		assert.equal(client.reloadResume.offer, null);
		assert.equal(client.autoPlayStall.current, null);
		assert.equal(client.tabLeadershipView.current.role, 'pending');
		for (const C of [
			client.ReloadResumeBanner,
			client.AutoPlayStallBanner,
			client.TabLeaderBanner
		]) {
			const host = mountBanner(tt, C);
			assert.equal(walk(host).filter((n) => n.hasAttribute('data-sel-node')).length, 0);
		}
		assert.deepEqual(registered(), []);
	});
	await t.test('a leader tab shows no tab-leader banner', (tt) => {
		client.tabLeadershipView.current = { role: 'leader', reason: null, leaseHolder: null };
		tt.after(() => {
			client.tabLeadershipView.current = { role: 'pending', reason: null, leaseHolder: null };
		});
		mountBanner(tt, client.TabLeaderBanner);
		assert.deepEqual(registered(), []);
	});
	await t.test('hiding a shown banner drops its nodes', (tt) => {
		showBanner(tt, 'reload-resume');
		assert.notDeepEqual(registered(), []);
		client.dismissReloadResume();
		client.flushSync();
		assert.deepEqual(registered(), []);
	});
	await t.test('unmounting the replay bar (the page hides it that way) drops its nodes', (tt) => {
		const host = document.createElement('div');
		document.body.appendChild(host);
		tt.after(() => host.remove());
		const props = { live: [], takenOverAll: false, onTakeOverAll: () => {} };
		const instance = client.mount(client.ReplayTakeoverBar, { target: host, props });
		client.flushSync();
		assert.notDeepEqual(registered(), []);
		client.unmount(instance);
		client.flushSync();
		assert.deepEqual(registered(), []);
	});
});

test('KEYB-14590 activate: the control in each banner leaf is the button that leaf names', async (t) => {
	// [if] a banner leaf is activated [then] the button it wraps is the control clicked, [else stop].
	const leafButton = (host, name, action) => {
		const [button] = buttonsIn(leafEl(host, client.ids.bannerLeafSelId(name, action)));
		return button;
	};
	await t.test('reload-resume.resume plays the offered decks', async (tt) => {
		const host = showBanner(tt, 'reload-resume');
		client.dispatchedCommands.length = 0;
		click(leafButton(host, 'reload-resume', 'resume'));
		for (let i = 0; i < 5; i++) await Promise.resolve();
		assert.deepEqual(client.dispatchedCommands, [
			{ type: 'play', deck: 1, playing: true },
			{ type: 'play', deck: 2, playing: true }
		]);
		assert.equal(client.reloadResume.offer, null);
	});
	await t.test('reload-resume.dismiss keeps the decks stopped', (tt) => {
		const host = showBanner(tt, 'reload-resume');
		client.dispatchedCommands.length = 0;
		click(leafButton(host, 'reload-resume', 'dismiss'));
		assert.equal(client.reloadResume.offer, null);
		assert.deepEqual(client.dispatchedCommands, []);
	});
	await t.test('autoplay-stall.tracks opens the track list', (tt) => {
		const host = showBanner(tt, 'autoplay-stall');
		const button = leafButton(host, 'autoplay-stall', 'tracks');
		assert.equal(button.getAttribute('aria-expanded'), 'false');
		click(button);
		assert.equal(button.getAttribute('aria-expanded'), 'true');
	});
	await t.test('autoplay-stall.dismiss hides the banner', (tt) => {
		const host = showBanner(tt, 'autoplay-stall');
		click(leafButton(host, 'autoplay-stall', 'dismiss'));
		assert.deepEqual(registered(), []);
		assert.notEqual(client.autoPlayStall.current, null, 'dismiss must not clear the durable stall');
	});
	await t.test('replay-takeover.take-over-all calls onTakeOverAll', (tt) => {
		let calls = 0;
		const host = mountBanner(tt, client.ReplayTakeoverBar, {
			live: [],
			takenOverAll: false,
			onTakeOverAll: () => void calls++
		});
		click(leafButton(host, 'replay-takeover', 'take-over-all'));
		assert.equal(calls, 1);
	});
	await t.test('tab-leader.take-control asks this tab to take over', (tt) => {
		const { leadership, dispose } = client.installTabLeadership();
		tt.after(dispose);
		const host = showBanner(tt, 'tab-leader');
		assert.equal(leadership.consumeTakeover(), false);
		click(leafButton(host, 'tab-leader', 'take-control'));
		assert.equal(leadership.consumeTakeover(), true);
	});
});
