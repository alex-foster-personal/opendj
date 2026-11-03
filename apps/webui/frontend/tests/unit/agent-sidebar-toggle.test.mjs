/**
 * The Cmd/Ctrl+I assistant sidebar toggle (src/lib/assistant/sidebar-toggle.ts).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { manualBootScheduler } from './fake-boot-scheduler.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let toggle;
let registry;
let flag;
let ssr;
let labels;

const MAC = 'MacIntel';
const PC = 'Win32';

before(async () => {
	toggle = await loadTypeScriptModule('src/lib/assistant/sidebar-toggle.ts');
	registry = await loadTypeScriptModule('src/lib/components/rb/hotkeys/hotkeys-registry.ts');
	flag = await loadTypeScriptModule('src/lib/assistant/sidebar-flag.ts');
	labels = await loadTypeScriptModule('src/lib/assistant/agent-labels.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as AgentSidebarMount } from '$lib/components/assistant/AgentSidebarMount.svelte';",
			"export { default as AgentSidebarView } from '$lib/components/assistant/AgentSidebarView.svelte';",
			"export * as stories from '$lib/components/assistant/AgentSidebarView.stories';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

const press = (over = {}) => ({
	key: 'i',
	code: 'KeyI',
	ctrlKey: false,
	metaKey: true,
	altKey: false,
	shiftKey: false,
	target: { tagName: 'DIV', getAttribute: () => null },
	...over
});

const ctrl = (over = {}) => press({ metaKey: false, ctrlKey: true, ...over });

test('Cmd+I toggles on Apple and Ctrl+I elsewhere, including a non-Latin layout that still reports KeyI', () => {
	assert.equal(toggle.isAssistantToggleKey(press(), MAC), true);
	assert.equal(toggle.isAssistantToggleKey(press({ key: 'ш' }), MAC), true);
	assert.equal(toggle.isAssistantToggleKey(ctrl({ key: 'I' }), PC), true);
	assert.equal(toggle.isAssistantToggleKey(ctrl({ key: 'ш' }), PC), true);
});

test("only the platform's own modifier toggles: the other one, or both, never do", () => {
	assert.equal(toggle.isAssistantToggleKey(ctrl(), MAC), false, 'Ctrl+I on a Mac');
	assert.equal(toggle.isAssistantToggleKey(press(), PC), false, 'Meta+I off a Mac');
	assert.equal(toggle.isAssistantToggleKey(press({ ctrlKey: true }), MAC), false, 'Ctrl+Cmd+I on a Mac');
	assert.equal(toggle.isAssistantToggleKey(press({ ctrlKey: true }), PC), false, 'Ctrl+Meta+I off a Mac');
});

test('a bare I, an extra modifier, or another letter never toggles', () => {
	assert.equal(toggle.isAssistantToggleKey(press({ metaKey: false }), MAC), false);
	assert.equal(toggle.isAssistantToggleKey(press({ shiftKey: true }), MAC), false);
	assert.equal(toggle.isAssistantToggleKey(press({ altKey: true }), MAC), false);
	assert.equal(toggle.isAssistantToggleKey(press({ key: 'u', code: 'KeyU' }), MAC), false);
});

test('typing in a field outside the sidebar never toggles', () => {
	for (const target of [
		{ tagName: 'TEXTAREA', closest: () => null },
		{ tagName: 'INPUT', type: 'text' },
		{ tagName: 'DIV', isContentEditable: true }
	]) {
		assert.equal(toggle.isAssistantToggleKey(press({ target }), MAC), false);
	}
});

test("the sidebar's own composer still takes the chord, so it closes from there", () => {
	const composer = {
		tagName: 'TEXTAREA',
		closest: (selector) => (selector === `[${toggle.ASSISTANT_ROOT_ATTR}]` ? { tagName: 'ASIDE' } : null)
	};
	assert.equal(toggle.isAssistantToggleKey(press({ target: composer }), MAC), true);
});

test('closing returns focus to the opener only while it is connected and outside the sidebar', () => {
	const at = (isConnected, inside = false) => ({
		isConnected,
		focus() {},
		closest: (selector) => (inside && selector === `[${toggle.ASSISTANT_ROOT_ATTR}]` ? {} : null)
	});
	const opener = at(true);
	assert.equal(toggle.focusReturnTarget(opener), opener);
	assert.equal(toggle.focusReturnTarget(at(false)), null);
	assert.equal(toggle.focusReturnTarget(at(true, true)), null);
	assert.equal(toggle.focusReturnTarget(null), null);
});

/** A focusable that records what happened to it. */
const focusable = ({ connected = true, inside = false, takesFocus = true } = {}) => ({
	isConnected: connected,
	focused: false,
	blurred: false,
	takesFocus,
	focus() {
		this.focused = this.takesFocus;
	},
	blur() {
		this.blurred = true;
	},
	closest: (selector) => (inside && selector === `[${toggle.ASSISTANT_ROOT_ATTR}]` ? {} : null)
});

test('AISIDE-7892f: a live opener gets focus back and nothing is blurred', () => {
	const opener = focusable();
	const close = focusable({ inside: true });
	toggle.settleFocusOnClose(opener, () => (opener.focused ? opener : close));
	assert.equal(opener.focused, true);
	assert.equal(close.blurred, false);
	assert.equal(opener.blurred, false);
});

test('AISIDE-7892f: with no opener left, focus does not stay on the hidden sidebar', () => {
	for (const opener of [null, focusable({ connected: false }), focusable({ inside: true })]) {
		const close = focusable({ inside: true });
		toggle.settleFocusOnClose(opener, () => close);
		assert.equal(close.blurred, true);
	}
});

test('AISIDE-7892f: an opener that cannot take focus, like the page body, still clears the sidebar', () => {
	const close = focusable({ inside: true });
	toggle.settleFocusOnClose(focusable({ takesFocus: false }), () => close);
	assert.equal(close.blurred, true);
});

test('AISIDE-7892f: closing never blurs what the user moved to outside the sidebar', () => {
	const elsewhere = focusable();
	toggle.settleFocusOnClose(null, () => elsewhere);
	assert.equal(elsewhere.blurred, false);
	assert.doesNotThrow(() => toggle.settleFocusOnClose(null, () => null), 'nothing focused is fine');
});

test('the chord is listed in the hotkeys overlay registry', () => {
	const entry = registry.HOTKEY_REGISTRY.find((e) => e.id === 'assistant-toggle');
	assert.equal(entry?.chord, registry.ASSISTANT_CHORD_LABEL);
});

test('AISIDE-7892j: the chord row shows only while the assistant flag is on', () => {
	const listed = (enabled) => registry.availableHotkeys(registry.HOTKEY_REGISTRY, enabled).map((e) => e.id);
	assert.equal(listed(true).length, registry.HOTKEY_REGISTRY.length);
	assert.ok(listed(true).includes('assistant-toggle'));
	assert.ok(!listed(false).includes('assistant-toggle'));
	assert.equal(listed(false).length, registry.HOTKEY_REGISTRY.length - 1, 'no other row disappears');
});

test('AISIDE-7892j: the sidebar shows only when the daemon declares assistant.sidebar on', () => {
	assert.equal(flag.ASSISTANT_SIDEBAR_FLAG_ID, 'assistant.sidebar');
	assert.equal(flag.assistantSidebarEnabledIn([]), false, 'flags unread: hidden');
	assert.equal(flag.assistantSidebarEnabledIn([{ flag_id: 'cmdk.v2', enabled: true }]), false, 'another flag on');
	assert.equal(flag.assistantSidebarEnabledIn([{ flag_id: 'assistant.sidebar', enabled: false }]), false);
	assert.equal(flag.assistantSidebarEnabledIn([{ flag_id: 'assistant.sidebar', enabled: true }]), true);
});

test('AISIDE-7892j: the chord is handled only while the flag is on', () => {
	assert.equal(toggle.assistantChordApplies(true, press(), MAC), true);
	assert.equal(toggle.assistantChordApplies(false, press(), MAC), false, 'flag off: the event passes through');
	assert.equal(toggle.assistantChordApplies(true, press({ key: 'j', code: 'KeyJ' }), MAC), false);
	assert.equal(toggle.assistantChordApplies(true, ctrl(), MAC), false, 'the platform rule still applies');
	assert.equal(toggle.assistantChordApplies(true, ctrl(), PC), true);
});

test('AISIDE-7892j: the mount renders its tab only while the flag is on', () => {
	const tabIn = (isEnabled) => ssr.render(ssr.AgentSidebarMount, { props: { isEnabled } }).body.includes('assistant-tab');
	assert.equal(tabIn(() => true), true);
	assert.equal(tabIn(() => false), false);
});

test('the chord label names Cmd on Apple platforms and Ctrl everywhere else', () => {
	for (const platform of ['MacIntel', 'iPhone', 'iPad']) {
		assert.equal(registry.assistantChordLabel(platform), 'Cmd+I');
	}
	for (const platform of ['Win32', 'Linux x86_64', '']) {
		assert.equal(registry.assistantChordLabel(platform), 'Ctrl+I');
	}
});

test('the announced shortcut is the one chord this platform takes, never both', () => {
	assert.equal(registry.assistantChordAria(MAC), 'Meta+I');
	assert.equal(registry.assistantChordAria(PC), 'Control+I');
	const body = ssr.render(ssr.AgentSidebarMount, { props: { isEnabled: () => true } }).body;
	assert.ok(body.includes(`aria-keyshortcuts="${registry.ASSISTANT_CHORD_ARIA}"`), body);
});

//----- the rendered view

const noop = () => {};
const view = (props) =>
	ssr.render(ssr.AgentSidebarView, {
		props: {
			items: [],
			busy: false,
			done: null,
			availability: 'ready',
			ontier: noop,
			onsend: noop,
			onapprove: noop,
			ondecline: noop,
			onstop: noop,
			onnewchat: noop,
			onclose: noop,
			...props
		}
	}).body;

test('AISIDE-7892e: Free preview shows only for the gateway; a user key bills the user', () => {
	assert.ok(view({ mode: 'gateway' }).includes('Free preview'));
	assert.ok(!view({ mode: 'byok' }).includes('Free preview'));
	assert.ok(!view({ mode: null }).includes('Free preview'));
	assert.ok(!view({}).includes('Free preview'), 'status unread claims nothing about billing');
});

test('AISIDE-7892i: a failed draft check is a note on the draft, not a disabled assistant', () => {
	const reason = 'the engine answered 503';
	const body = view({ availability: 'keyless', draft: 'pause deck 1', instantError: reason });
	assert.ok(body.includes(labels.instantCheckFailedNote(reason)), 'the failure is shown, with its reason');
	assert.ok(!/<textarea[^>]*\sdisabled/.test(body), 'the composer stays open for the edit that retries');
	assert.ok(!body.includes(labels.AVAILABILITY_NOTICE.unreachable), 'the assistant is not reported down');
	assert.ok(body.includes('only instant commands work'), 'still the keyless notice');
});

test('a keyless draft with no check failure carries no check note (control)', () => {
	const body = view({ availability: 'keyless', draft: 'pause deck 1', instantDraft: true });
	assert.ok(!body.includes('Could not check'));
});

test('AISIDE-7892i: a failed check refuses the draft instead of queueing it forever', () => {
	const at = (over) => ({
		draft: 'pause deck 1',
		composerOpen: true,
		busy: false,
		held: false,
		keyless: true,
		instantDraft: null,
		instantCheckFailed: false,
		...over
	});
	assert.equal(labels.sendOutcome(at({})), 'queue', 'still asking: queued, as before');
	assert.equal(labels.sendOutcome(at({ instantCheckFailed: true })), 'refuse');
	assert.equal(labels.sendOutcome(at({ instantCheckFailed: true, keyless: false })), 'send', 'a model needs no check');
	assert.equal(labels.sendOutcome(at({ instantCheckFailed: true, instantDraft: true })), 'send');
});

//----- the recorded stories

/** The shipped tools' argument names, one row per tool the model can call. */
const TOOL_ARGUMENTS = {
	status: [],
	app_state: [],
	library_read: ['path'],
	library_write: ['method', 'path', 'body'],
	command: ['verb', 'order'],
	system_volume_get: [],
	system_volume_set: ['volume'],
	usb_list: [],
	usb_eject: ['volume_id']
};
const REQUIRED_ARGUMENTS = { library_read: ['path'], system_volume_set: ['volume'], usb_eject: ['volume_id'] };

test('every story tool call uses the argument names of the shipped tool', () => {
	const tools = Object.values(ssr.stories)
		.flatMap((story) => story?.args?.items ?? [])
		.filter((item) => item.kind === 'tool');
	assert.ok(tools.length >= 8, `the stories must record tool calls, found ${tools.length}`);
	const names = new Set(tools.map((tool) => tool.call.name));
	for (const name of ['library_read', 'system_volume_set', 'command', 'app_state']) assert.ok(names.has(name), name);
	for (const { call } of tools) {
		const allowed = TOOL_ARGUMENTS[call.name];
		assert.ok(allowed, `${call.name} is not a shipped tool`);
		const given = Object.keys(call.arguments);
		for (const key of given) assert.ok(allowed.includes(key), `${call.name} has no ${key} argument`);
		for (const key of REQUIRED_ARGUMENTS[call.name] ?? []) assert.ok(given.includes(key), `${call.name} needs ${key}`);
		if (call.name === 'command') assert.equal(given.length, 1, 'command takes exactly one of verb or order');
	}
});

test('the Free preview badge in stories comes from the same mode prop', () => {
	assert.equal(ssr.stories.default.args.mode, 'gateway');
	assert.equal(ssr.stories.OwnKey.args.mode, 'byok');
});

//----- the deferred mount

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test('the mount import does not start until the boot window releases it', async () => {
	const { scheduler, release } = manualBootScheduler();
	let loads = 0;
	const ready = [];
	toggle.deferLoad(
		'assistant-mount:load',
		async () => {
			loads += 1;
			return 'mount';
		},
		(loaded) => ready.push(loaded),
		(error) => assert.fail(`a successful load must not report an error: ${error}`),
		scheduler
	);
	await settle();
	assert.equal(loads, 0, 'nothing runs during first paint');
	release();
	await settle();
	assert.equal(loads, 1);
	assert.deepEqual(ready, ['mount']);
});

test('a chunk that fails, or a mount that throws, reaches onError instead of vanishing', async () => {
	const { scheduler, release } = manualBootScheduler();
	const errors = [];
	const broken = new Error('chunk 404');
	toggle.deferLoad('a', () => Promise.reject(broken), () => assert.fail('never ready'), (e) => errors.push(e), scheduler);
	const threw = new Error('mount threw');
	toggle.deferLoad('b', async () => 'x', () => { throw threw; }, (e) => errors.push(e), scheduler);
	release();
	await settle();
	assert.deepEqual(errors, [broken, threw]);
});
