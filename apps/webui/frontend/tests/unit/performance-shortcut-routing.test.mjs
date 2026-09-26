/**
 * Issue #3528: /performance global shortcuts fire except in real text entry.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let routing;
let feedbackStore;
let textEntry;

function target(tagName, overrides = {}) {
	return {
		tagName,
		type: overrides.type ?? null,
		isContentEditable: overrides.contentEditable ?? false,
		getAttribute: (name) => (name === 'role' ? (overrides.role ?? null) : null)
	};
}

// isNativeInteractiveTarget checks `instanceof Element`/`HTMLElement`, so the
// plain-object target() stand-in above always reads as non-interactive to it
// (which is why it never influences the existing M/Space cases below). Tab's
// own tests need a target that genuinely is one, matching the fixture shape
// restored in performance-hotkeys-target.test.mjs.
class FakeElement {}
class FakeHTMLElement extends FakeElement {}

function nativeTarget(tagName, { role = null, tabindex = false, href = false } = {}) {
	const el = new FakeHTMLElement();
	el.tagName = tagName;
	el.isContentEditable = false;
	el.getAttribute = (name) => (name === 'role' ? role : name === 'tabindex' ? tabindex : null);
	el.hasAttribute = (name) =>
		name === 'href' ? href : name === 'tabindex' ? tabindex !== false : false;
	return el;
}

function key(overrides = {}) {
	let prevented = false;
	return {
		code: overrides.code ?? 'KeyM',
		key: overrides.key ?? 'm',
		target: overrides.target ?? target('BODY'),
		metaKey: overrides.metaKey ?? false,
		ctrlKey: overrides.ctrlKey ?? false,
		altKey: overrides.altKey ?? false,
		shiftKey: overrides.shiftKey ?? false,
		timeStamp: overrides.timeStamp ?? 42,
		preventDefault: () => {
			prevented = true;
		},
		wasPrevented: () => prevented
	};
}

before(async () => {
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = FakeHTMLElement;
	routing = await loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts');
	feedbackStore = await loadTypeScriptModule('src/lib/rb/feedback-store.svelte.ts');
	textEntry = await loadTypeScriptModule('src/lib/keyboard/text-entry-target.ts');
	feedbackStore.feedbackState.availability = 'ok';
	feedbackStore.feedbackState.placementArmed = false;
});

test('M routes on library row, button, range slider, and body', () => {
	for (const focus of [
		target('TR', { role: 'row' }),
		target('BUTTON'),
		target('INPUT', { type: 'range' }),
		target('BODY')
	]) {
		assert.deepEqual(routing.resolvePerformanceShortcutAction(key({ target: focus })), { kind: 'm' });
	}
});

test('M does not route in text input, textarea, or contenteditable', () => {
	for (const focus of [
		target('INPUT', { type: 'text' }),
		target('TEXTAREA'),
		target('DIV', { contentEditable: true })
	]) {
		assert.equal(routing.resolvePerformanceShortcutAction(key({ target: focus })), null);
	}
});

test('Cmd+Shift+M routes in textarea as backup chord', () => {
	assert.deepEqual(
		routing.resolvePerformanceShortcutAction(
			key({ target: target('TEXTAREA'), metaKey: true, shiftKey: true })
		),
		{ kind: 'm' }
	);
});

test('M invokes armPinPlacement on non-text targets and not in text entry', () => {
	for (const focus of [
		target('TR', { role: 'row' }),
		target('BUTTON'),
		target('INPUT', { type: 'range' }),
		target('BODY')
	]) {
		feedbackStore.feedbackState.placementArmed = false;
		const event = key({ target: focus });
		assert.equal(
			routing.handlePerformanceShortcutKeydown(event, {
				toggleRecentPlay: () => assert.fail('Space handler must not run for M'),
				toggleNextOnlyFilter: () => assert.fail('Tab handler must not run for M'),
				resizeLast: () => assert.fail('loop resize must not run for M'),
				exitLast: () => assert.fail('loop exit must not run for M'),
				armPinPlacement: feedbackStore.armPinPlacement
			}),
			true
		);
		assert.equal(event.wasPrevented(), true);
		assert.equal(feedbackStore.feedbackState.placementArmed, true);
	}

	for (const focus of [
		target('INPUT', { type: 'text' }),
		target('TEXTAREA'),
		target('DIV', { contentEditable: true })
	]) {
		feedbackStore.feedbackState.placementArmed = false;
		const event = key({ target: focus });
		assert.equal(
			routing.handlePerformanceShortcutKeydown(event, {
				toggleRecentPlay: () => assert.fail('must not run in text entry'),
				toggleNextOnlyFilter: () => assert.fail('must not run in text entry'),
				resizeLast: () => assert.fail('must not run in text entry'),
				exitLast: () => assert.fail('must not run in text entry'),
				armPinPlacement: () => assert.fail('must not run in text entry')
			}),
			false
		);
		assert.equal(event.wasPrevented(), false);
		assert.equal(feedbackStore.feedbackState.placementArmed, false);
	}
});

test('Space prevents default and runs the app action on library list and row', () => {
	for (const focus of [target('DIV'), target('TR', { role: 'row' })]) {
		const calls = [];
		const event = key({ code: 'Space', key: ' ', target: focus });
		assert.equal(
			routing.handlePerformanceShortcutKeydown(event, {
				toggleRecentPlay: (pressT0Ms, quantize) => {
					calls.push({ pressT0Ms, quantize });
				},
				toggleNextOnlyFilter: () => assert.fail('Tab handler must not run for Space'),
				resizeLast: () => assert.fail('loop resize must not run for Space'),
				exitLast: () => assert.fail('loop exit must not run for Space'),
				armPinPlacement: () => assert.fail('M handler must not run for Space')
			}),
			true
		);
		assert.equal(event.wasPrevented(), true);
		assert.deepEqual(calls, [{ pressT0Ms: 42, quantize: false }]);
	}
});

test('Space leaves a focused text input alone', () => {
	const calls = [];
	const event = key({ code: 'Space', key: ' ', target: target('INPUT', { type: 'text' }) });
	assert.equal(
		routing.handlePerformanceShortcutKeydown(event, {
			toggleRecentPlay: () => calls.push('space'),
			toggleNextOnlyFilter: () => calls.push('tab'),
			resizeLast: () => calls.push('resize'),
			exitLast: () => calls.push('exit'),
			armPinPlacement: () => calls.push('m')
		}),
		false
	);
	assert.equal(event.wasPrevented(), false);
	assert.deepEqual(calls, []);
});

// r3549 review P1 BLOCKING: the amended A11Y-01 gate narrowed Tab (like every
// other key) to isTextEntryTarget alone, which let Tab fire - and
// preventDefault - on a focused button, link, library row, or range slider.
// Real Tab focus movement stopped working there, trapping a keyboard user on
// the first control they landed on. Tab is the one key that still needs the
// broader native-interactive exemption; Space/M/loop-resize deliberately do
// not (A11Y-01 wants those to fire over buttons and sliders).
test('Tab does not trap focus on a native-interactive target (P1 BLOCKING regression)', () => {
	for (const focus of [
		nativeTarget('BUTTON'),
		nativeTarget('A', { href: true }),
		nativeTarget('INPUT', { role: 'range' }),
		nativeTarget('DIV', { tabindex: '0' }),
		nativeTarget('TR', { role: 'row', tabindex: '0' })
	]) {
		const event = key({ code: 'Tab', key: 'Tab', target: focus });
		assert.equal(
			routing.resolvePerformanceShortcutAction(event),
			null,
			`Tab must not route on a focused ${focus.tagName}`
		);
		assert.equal(
			routing.handlePerformanceShortcutKeydown(event, {
				toggleRecentPlay: () => assert.fail('Space handler must not run for Tab'),
				toggleNextOnlyFilter: () => assert.fail('Tab must not trap focus on a native control'),
				resizeLast: () => assert.fail('loop resize must not run for Tab'),
				exitLast: () => assert.fail('loop exit must not run for Tab'),
				armPinPlacement: () => assert.fail('M handler must not run for Tab')
			}),
			false
		);
		assert.equal(event.wasPrevented(), false, `Tab must reach the browser's own focus move on ${focus.tagName}`);
	}
});

test('Tab still toggles the next-only filter on non-interactive page chrome', () => {
	for (const focus of [target('BODY'), target('DIV'), nativeTarget('DIV')]) {
		const calls = [];
		const event = key({ code: 'Tab', key: 'Tab', target: focus });
		assert.deepEqual(routing.resolvePerformanceShortcutAction(event), { kind: 'tab' });
		assert.equal(
			routing.handlePerformanceShortcutKeydown(event, {
				toggleRecentPlay: () => assert.fail('Space handler must not run for Tab'),
				toggleNextOnlyFilter: () => calls.push('tab'),
				resizeLast: () => assert.fail('loop resize must not run for Tab'),
				exitLast: () => assert.fail('loop exit must not run for Tab'),
				armPinPlacement: () => assert.fail('M handler must not run for Tab')
			}),
			true
		);
		assert.equal(event.wasPrevented(), true);
		assert.deepEqual(calls, ['tab']);
	}
});

test('performance-hotkeys uses the shared predicate via routing, not a second focus check', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /handlePerformanceShortcutKeydown/);
	assert.doesNotMatch(source, /isNativeInteractiveTarget/);
	assert.match(
		await readFile('src/lib/rb/performance-shortcut-routing.ts', 'utf8'),
		/isTextEntryTarget/
	);
	assert.equal(textEntry.isTextEntryTarget(target('INPUT', { type: 'text' })), true);
});
