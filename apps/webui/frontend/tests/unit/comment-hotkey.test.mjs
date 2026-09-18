/**
 * The comment hotkey, and the one shortcut allowed inside a text field.
 *
 * Issue #3528: global shortcuts use the shared text-entry predicate, not the
 * old native-interactive guard.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function read(rel) {
	return readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');
}

const hotkeys = read('src/lib/rb/performance-hotkeys.ts');
const routing = read('src/lib/rb/performance-shortcut-routing.ts');
const draftBubble = read('src/lib/components/rb/FeedbackPinDraftBubble.svelte');

test("'m' arms comment pin placement", () => {
	assert.match(hotkeys, /armPinPlacement/);
	assert.match(routing, /kind: 'm'/);
});

test('no shortcut fires while a text field has focus, or with a modifier held', () => {
	assert.match(routing, /if \(isTextEntryTarget\(e\.target\)\) return null;/);
	assert.match(routing, /if \(isTextEntryTarget\(e\.target\) \|\| e\.metaKey \|\| e\.ctrlKey \|\| e\.altKey\) return null;/);
	assert.doesNotMatch(hotkeys, /isNativeInteractiveTarget/);
});

test('Cmd/Ctrl+Enter saves the comment being typed', () => {
	const at = draftBubble.indexOf('class="fb-bubble-text"');
	assert.ok(at > 0, 'the comment textarea moved');
	const tag = draftBubble.slice(at, draftBubble.indexOf('></textarea>', at));
	assert.match(tag, /e\.key === 'Enter' && \(e\.metaKey \|\| e\.ctrlKey\)/);
	assert.match(tag, /savePinDraft\(\)/);
});
