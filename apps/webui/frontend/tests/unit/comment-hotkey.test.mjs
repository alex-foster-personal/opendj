/**
 * The comment hotkey, and the one shortcut allowed inside a text field.
 *
 * SOURCE-SHAPE (see capability-gating-markup.test.mjs): the hotkey handler is a
 * closure inside installPerformanceHotkeys with no seam, and extracting one
 * mid-review would put every transport key at risk for a one-key addition.
 *
 * Pin 919d65b350b1 (the maintainer, Wed 2 Sep 2026): "M shortcut when not in text input
 * or similar (no shortcuts should work except cmd enter for submit probably
 * when in text input/area as requirement rule) m is shortcut for clicking the
 * comment icon."
 *
 * The requirement's first half was ALREADY met and is asserted here so it stays
 * met: isNativeInteractiveTarget gates the whole handler, and any modifier returns early.
 *
 * Regression lines:
 * - if 'm' stops arming pin placement then dropping a comment means reaching
 *   for the topbar icon again
 * - if the typing guard stops preceding the key dispatch then typing the letter
 *   m into a comment arms a pin placement over the top of it
 * - if Cmd+Enter stops saving then the rule says one shortcut works in a text
 *   field and none does
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function read(rel) {
	return readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');
}

const hotkeys = read('src/lib/rb/performance-hotkeys.ts');
// #3892 moved the `m` pin hotkey out of the /performance-only handler into its
// own window-wide installer, so pins can be dropped on every route.
const pinHotkeys = read('src/lib/rb/comment-pin-hotkeys.ts');
const widget = read('src/lib/components/rb/FeedbackWidget.svelte');
// savePinDraft's textarea moved into its own component (Thu 3 Sep 2026,
// pin review v2) to bring FeedbackWidget.svelte back under the 600-line
// file-size gate.
const draftBubble = read('src/lib/components/rb/FeedbackPinDraftBubble.svelte');

test("'m' arms comment pin placement", () => {
	assert.match(pinHotkeys, /e\.key !== 'm' && e\.key !== 'M'\) return;/);
	assert.match(pinHotkeys, /if \(e\.metaKey \|\| e\.ctrlKey \|\| e\.altKey\) return;/);
	assert.match(pinHotkeys, /if \(isNativeInteractiveTarget\(e\.target\)\) return;/);
	assert.match(pinHotkeys, /armPinPlacement\(\)/);
	assert.doesNotMatch(hotkeys, /armPinPlacement/, 'one owner for the m key, or it arms twice');
});

test('no shortcut fires while a text field has focus, or with a modifier held', () => {
	const body = hotkeys.slice(hotkeys.indexOf('const onKey ='));
	// LATENCY-02 (c8d78c993) hoisted the Space branch above the shared guard so
	// Cmd/Ctrl+Space can launch quantized. That branch therefore carries its
	// own typing guard, which must be its FIRST statement: before any modifier
	// handling, preventDefault or transport dispatch.
	const spaceOpen = body.indexOf("if (e.code === 'Space' || e.key === ' ') {");
	assert.ok(spaceOpen > 0, 'the Space branch moved');
	const spaceBody = body.slice(spaceOpen + "if (e.code === 'Space' || e.key === ' ') {".length);
	assert.match(
		spaceBody,
		/^\s*if \(isNativeInteractiveTarget\(e\.target\)\) return;/,
		'the typing guard no longer precedes the Space dispatch'
	);
	// Every other key goes through the shared guard, which must come before the
	// first non-Space dispatch.
	const guard = body.indexOf('if (isNativeInteractiveTarget(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;');
	const firstKey = body.indexOf("e.key === 'Tab'");
	assert.ok(guard > spaceOpen && firstKey > guard, 'the typing guard no longer precedes the dispatch');
});

test('Cmd/Ctrl+Enter saves the comment being typed', () => {
	const at = draftBubble.indexOf('class="fb-bubble-text"');
	assert.ok(at > 0, 'the comment textarea moved');
	const tag = draftBubble.slice(at, draftBubble.indexOf('></textarea>', at));
	assert.match(tag, /e\.key === 'Enter' && \(e\.metaKey \|\| e\.ctrlKey\)/);
	assert.match(tag, /savePinDraft\(\)/);
});
