/**
 * Sidebar labels and the held-call keyboard rule (src/lib/assistant/agent-labels.ts).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let labels;

before(async () => {
	labels = await loadTypeScriptModule('src/lib/assistant/agent-labels.ts');
});

const facts = (over = {}) => ({
	key: 'Enter',
	modified: false,
	inSidebar: true,
	editing: false,
	onButton: false,
	...over
});

test('Enter approves and Esc declines from inside the sidebar', () => {
	assert.equal(labels.heldKeyAction(facts()), 'approve');
	assert.equal(labels.heldKeyAction(facts({ key: 'Escape' })), 'decline');
});

test('keys from outside the sidebar are never taken', () => {
	assert.equal(labels.heldKeyAction(facts({ inSidebar: false })), null);
	assert.equal(labels.heldKeyAction(facts({ key: 'Escape', inSidebar: false })), null);
});

test('typing in a field and modified keys are left alone', () => {
	for (const key of ['Enter', 'Escape']) {
		assert.equal(labels.heldKeyAction(facts({ key, editing: true })), null);
		assert.equal(labels.heldKeyAction(facts({ key, modified: true })), null);
	}
});

test("a focused button keeps its own Enter, so Enter on Don't never approves", () => {
	assert.equal(labels.heldKeyAction(facts({ onButton: true })), null);
	assert.equal(labels.heldKeyAction(facts({ key: 'Escape', onButton: true })), 'decline');
});

test('other keys do nothing', () => {
	assert.equal(labels.heldKeyAction(facts({ key: ' ' })), null);
	assert.equal(labels.heldKeyAction(facts({ key: 'a' })), null);
});

test('one status reading is one availability: an error wins, then no reading, then configured', () => {
	assert.equal(labels.availabilityOf(null, null), 'checking');
	assert.equal(labels.availabilityOf(null, 'connection refused'), 'unreachable');
	assert.equal(labels.availabilityOf({ configured: true }, 'preview failed'), 'unreachable');
	assert.equal(labels.availabilityOf({ configured: false }, null), 'keyless');
	assert.equal(labels.availabilityOf({ configured: true }, null), 'ready');
});

test('every non-ready availability has a notice, and keyless names what still works', () => {
	assert.deepEqual(Object.keys(labels.AVAILABILITY_NOTICE).sort(), ['checking', 'keyless', 'unreachable']);
	assert.match(labels.AVAILABILITY_NOTICE.keyless, /only instant commands work/);
	assert.match(labels.KEYLESS_SEND_TITLE, /only instant commands work/);
});

test('a setup refusal reads as the keyless notice, never the raw env var names', () => {
	for (const code of labels.SETUP_CODES) {
		const text = labels.errorText(code, 'set OPENROUTER_API_KEY or MDT_AI_GATEWAY_URL');
		assert.equal(text, labels.AVAILABILITY_NOTICE.keyless);
		assert.doesNotMatch(text, /[A-Z]+_[A-Z_]+/);
	}
	assert.equal(labels.errorText('assistant_upstream_error', 'The model service answered 502.'), 'The model service answered 502.');
});

test('Esc in the composer closes only when the draft is empty and nothing is held, else blurs', () => {
	assert.equal(labels.composerEscapeAction({ draft: '', held: false }), 'close');
	assert.equal(labels.composerEscapeAction({ draft: '  \n', held: false }), 'close');
	assert.equal(labels.composerEscapeAction({ draft: 'pause deck 4', held: false }), 'blur');
	assert.equal(labels.composerEscapeAction({ draft: '', held: true }), 'blur');
});

test('Enter on a keyless draft still being checked queues, then resolves to send or refuse, never vanishes', () => {
	const at = (over = {}) => ({
		draft: 'master volume 40%',
		composerOpen: true,
		busy: false,
		held: false,
		keyless: true,
		instantDraft: null,
		...over
	});
	assert.equal(labels.sendOutcome(at()), 'queue');
	assert.equal(labels.sendOutcome(at({ instantDraft: true })), 'send');
	assert.equal(labels.sendOutcome(at({ instantDraft: false })), 'refuse');
	assert.equal(labels.sendOutcome(at({ keyless: false })), 'send', 'with a model every draft sends');
	for (const over of [{ draft: '  ' }, { busy: true }, { held: true }, { composerOpen: false }]) {
		assert.equal(labels.sendOutcome(at(over)), 'ignore');
	}
});

test('a context ref becomes a chip with a kind-qualified id and title', () => {
	assert.deepEqual(labels.contextChip({ kind: 'usb', id: 'vol-1', label: 'CDJ-SET' }), {
		id: 'usb:vol-1',
		label: 'CDJ-SET',
		title: 'USB stick: CDJ-SET'
	});
});

test('known tools get words, unknown tools show their raw name', () => {
	assert.equal(labels.toolLabel('library_read'), 'Search library');
	assert.equal(labels.toolLabel('shell_run'), 'shell_run');
	assert.deepEqual(Object.keys(labels.TIER_LABEL), ['instant', 'fast', 'think']);
});
