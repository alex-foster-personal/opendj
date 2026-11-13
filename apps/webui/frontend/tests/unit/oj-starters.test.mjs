/**
 * OJ starter chips (issue #14040): an empty agent sidebar suggests what to ask, keyed by route,
 * through the pure oj-starters.ts module, an SSR render of OjStarterChips and of the real
 * AgentSidebar container, and a client mount of the real view with the chips in its slot.
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';
import { bundleOjStarterChipsClient } from './mount-svelte-client.mjs';
import { installQualityBadgeDom } from './quality-badge-client-harness.mjs';

const FAKE_PROBE = fileURLToPath(new URL('./fake-assistant-status-probe.mjs', import.meta.url));

let starters;
let policy;
let ssr;
let client;
let teardownDom;

before(async () => {
	starters = await loadTypeScriptModule('src/lib/assistant/oj-starters.ts');
	policy = await loadTypeScriptModule('src/lib/components/assistant/oj-motion-policy.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as AgentSidebar } from '$lib/components/assistant/AgentSidebar.svelte';",
			"export { default as AgentSidebarView } from '$lib/components/assistant/AgentSidebarView.svelte';",
			"export { default as OjStarterChips } from '$lib/components/assistant/OjStarterChips.svelte';",
			"export { fakeAssistantProbe } from '$lib/assistant/status-probe.svelte';",
			"export { render } from 'svelte/server';",
			"export { createRawSnippet } from 'svelte';"
		].join('\n'),
		{ alias: { '$lib/assistant/status-probe.svelte': FAKE_PROBE } }
	);
	teardownDom = installQualityBadgeDom();
	client = await bundleOjStarterChipsClient();
});

after(() => {
	teardownDom?.();
});

const noop = () => {};
const MOTION_INPUT = {
	anyDeckPlaying: false,
	performanceVisible: true,
	reducedMotion: false,
	streaming: false
};
const LIVE = () => policy.ojMotionPolicy(MOTION_INPUT);
const STILL_REDUCED = () => policy.ojMotionPolicy({ ...MOTION_INPUT, reducedMotion: true });
const STILL_PLAYING = () => policy.ojMotionPolicy({ ...MOTION_INPUT, anyDeckPlaying: true });

function renderChips(list, motion) {
	return ssr.render(ssr.OjStarterChips, { props: { starters: list, motion, onpick: noop } }).body;
}

function chipTags(html) {
	return html.match(/<button[^>]*data-oj-starter=[^>]*>/g) ?? [];
}

function findAll(root, predicate, out = []) {
	if (predicate(root)) out.push(root);
	for (const child of root.childNodes ?? []) findAll(child, predicate, out);
	return out;
}

function click(node) {
	node.dispatchEvent({
		type: 'click',
		bubbles: true,
		cancelBubble: false,
		defaultPrevented: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {
			this.cancelBubble = true;
		}
	});
}

test('an empty conversation on /performance shows a deck or transition chip', () => {
	/** [if] the conversation is empty on /performance [then] at least one deck or transition chip shows, [else stop]. */
	const list = starters.ojStartersFor('/performance', []);
	assert.ok(list.length >= 3 && list.length <= 5, `3 to 5 chips, got ${list.length}`);
	assert.ok(['deck', 'transition'].includes(list[0].kind), 'performance leads with a deck prompt');
	const html = renderChips(list, LIVE());
	assert.equal(chipTags(html).length, list.length);
	assert.match(html, /data-oj-starter-kind="(deck|transition)"/);
	assert.match(html, /Find tracks like the one on deck 1/);
});

test('the library route leads with library prompts and keeps 3 to 5 chips', () => {
	/** [if] the conversation is empty outside /performance [then] the list leads with set or library prompts, [else stop]. */
	for (const path of ['/', '/library', '']) {
		const list = starters.ojStartersFor(path, []);
		assert.ok(list.length >= 3 && list.length <= 5, `${path}: 3 to 5 chips`);
		assert.ok(['set', 'library'].includes(list[0].kind), `${path}: leads with a library prompt`);
	}
	assert.equal(starters.ojStarterRoute('/performance'), 'performance');
	assert.equal(starters.ojStarterRoute('/'), 'library');
});

test('the real container mounts the chips in the empty log', () => {
	/** [if] AgentSidebar renders ready with an empty session [then] its log holds the starter chips, [else stop]. */
	ssr.fakeAssistantProbe.status = { configured: true, mode: 'byok' };
	try {
		const html = ssr.render(ssr.AgentSidebar, { props: { open: true, onclose: noop } }).body;
		const tags = chipTags(html);
		assert.equal(tags.length, starters.OJ_STARTERS.library.length);
		assert.match(html, /role="group" aria-label="Things to ask OJ"/);
		assert.doesNotMatch(html, /Find techno around 128 BPM/, 'the old sending suggestions are gone');
	} finally {
		ssr.fakeAssistantProbe.status = null;
	}
});

test('a chip click fills the composer and sends nothing', () => {
	/** [if] a chip is clicked [then] the composer holds its text and nothing is sent, [else stop]. */
	const { OjStarterChips, mount, unmount, flushSync } = client;
	const target = document.createElement('div');
	document.body.appendChild(target);
	const picked = [];
	const list = starters.OJ_STARTERS.performance;
	const instance = mount(OjStarterChips, {
		target,
		props: { starters: list, motion: LIVE(), onpick: (text) => picked.push(text) }
	});
	let draft;
	try {
		flushSync();
		const chips = findAll(target, (node) => node.getAttribute?.('data-oj-starter') != null);
		assert.equal(chips.length, list.length);
		assert.equal(chips[1].getAttribute('type'), 'button', 'a chip never submits the composer');
		click(chips[1]);
		flushSync();
		assert.deepEqual(picked, [list[1].text]);
		draft = picked[0];
	} finally {
		unmount(instance);
		target.remove();
	}
	const sent = [];
	const html = ssr.render(ssr.AgentSidebarView, {
		props: { ...viewProps(), items: [], draft, onsend: (text) => sent.push(text) }
	}).body;
	const composer = html.match(/<textarea[^>]*>([^<]*)<\/textarea>/);
	assert.ok(composer, 'the composer textarea renders');
	assert.equal(composer[1], list[1].text);
	assert.deepEqual(sent, []);
});

function viewProps() {
	return {
		busy: false,
		done: null,
		availability: 'ready',
		ontier: noop,
		onsend: noop,
		onapprove: noop,
		ondecline: noop,
		onstop: noop,
		onnewchat: noop,
		onclose: noop
	};
}

test('a conversation with a message renders no chips', () => {
	/** [if] the conversation has a message [then] no chips render, [else stop]. */
	const items = [{ kind: 'user', text: 'hello' }];
	assert.deepEqual(starters.ojStartersFor('/performance', items), []);
	assert.deepEqual(starters.ojStartersFor('/', items), []);
	assert.equal(chipTags(renderChips([], LIVE())).length, 0);
	assert.doesNotMatch(renderChips([], LIVE()), /oj-starters/);
	const slot = ssr.createRawSnippet(() => ({ render: () => '<b data-slot="starters"></b>' }));
	const props = { ...viewProps(), done: 'stop', starters: slot };
	const empty = ssr.render(ssr.AgentSidebarView, { props: { ...props, items: [] } }).body;
	assert.match(empty, /data-slot="starters"/, 'the view draws the slot while the log is empty');
	const busy = ssr.render(ssr.AgentSidebarView, { props: { ...props, items } }).body;
	assert.doesNotMatch(busy, /data-slot="starters"/);
});

test('reduced motion or a playing deck on /performance renders chips with no stagger', () => {
	/** [if] reduced motion is on or a deck plays on /performance [then] chips render with no stagger, [else stop]. */
	const list = starters.OJ_STARTERS.performance;
	for (const motion of [STILL_REDUCED(), STILL_PLAYING()]) {
		assert.equal(starters.ojStarterStaggerMs(motion), 0);
		const tags = chipTags(renderChips(list, motion));
		assert.equal(tags.length, list.length);
		for (const tag of tags) {
			assert.doesNotMatch(tag, /animation-delay/);
			assert.doesNotMatch(tag, /class="[^"]*\benter\b/);
		}
	}
	const live = chipTags(renderChips(list, LIVE()));
	const step = starters.OJ_STARTER_STAGGER_MS;
	assert.ok(step > 0);
	live.forEach((tag, index) => {
		assert.match(tag, /class="[^"]*\benter\b/);
		assert.match(tag, new RegExp(`animation-delay: ${index * step}ms`));
	});
});
