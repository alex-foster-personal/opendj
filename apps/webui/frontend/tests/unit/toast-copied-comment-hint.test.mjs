/**
 * @pytest.mark.requirement PVPIN-20
 * Pin 30de7a76291f: clicking a toast copies it, and the copied confirmation
 * also tells the operator to press M to leave a comment for the developer.
 *
 * [if] the copied note renders [then] it carries the press-M hint [else stop].
 * [if] M is pressed with focus on the toast button just clicked [then]
 *   comment-pin placement arms, same as any normal comment [else stop].
 * [if] the note is cleared too fast to read [then] the hint is decorative
 *   [else stop].
 *
 * Regression lines:
 * - if the copied note only says "copied" then the operator is never told
 *   the next step -> broken
 * - if M on the focused toast button does not arm placement then the hint
 *   lies -> broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (p) => readFileSync(fileURLToPath(new URL(`../../${p}`, import.meta.url)), 'utf8');

class FakeElement {}
class FakeHTMLElement extends FakeElement {}

let policy;
let routing;
before(async () => {
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = FakeHTMLElement;
	policy = await loadTypeScriptModule('src/lib/toast-tray-policy.ts');
	routing = await loadTypeScriptModule('src/lib/rb/comment-pin-hotkey-routing.ts');
});

test('the copied note says to press M to leave a comment for the developer', () => {
	assert.match(policy.TOAST_COPIED_NOTE, /^copied\b/);
	assert.match(policy.TOAST_COPIED_NOTE, /press M to leave a comment for the developer/);
});

test('the copied note stays long enough to read', () => {
	assert.ok(policy.TOAST_COPIED_NOTE_MS >= 4000, String(policy.TOAST_COPIED_NOTE_MS));
});

test('ToastStack renders the policy note and timing, not a bare "copied"', () => {
	const stack = read('src/lib/components/rb/ToastStack.svelte');
	const noteAt = stack.indexOf('data-toast-copied=');
	assert.ok(noteAt > 0, 'copied note marker missing (control)');
	const note = stack.slice(noteAt, stack.indexOf('</span>', noteAt));
	assert.match(note, /\{TOAST_COPIED_NOTE\}/);
	assert.doesNotMatch(note, />copied$/);
	assert.match(stack, /\},\s*TOAST_COPIED_NOTE_MS\)/);
});

test('M on the toast button that was just clicked arms comment-pin placement', () => {
	const button = new FakeHTMLElement();
	button.tagName = 'BUTTON';
	button.type = 'button';
	button.isContentEditable = false;
	button.getAttribute = () => null;
	button.hasAttribute = () => false;
	const press = (key) =>
		routing.resolveCommentPinHotkey({
			key,
			target: button,
			metaKey: false,
			ctrlKey: false,
			altKey: false,
			shiftKey: false
		});
	assert.equal(press('m'), 'arm');
	assert.equal(press('M'), 'arm');
	// Control: the same press with a different key does nothing.
	assert.equal(press('n'), null);
});
