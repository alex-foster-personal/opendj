import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * DECKUX-01 - creating an empty hot-cue slot opens a focused text input for
 * its name. The operator can continue typing immediately, and blur commits
 * exactly the draft that has been entered so far.
 *
 * Regression lines:
 * - if an empty-slot save does not enter rename mode after the real save then broken
 * - if rename mode does not await the DOM update before focusing then broken
 * - if the name input loses the entered draft on blur then broken
 * - if a replacement track receives a pending name edit then broken
 * - if a blur save queues ahead of a Class A seek then broken
 * - if a hot-cue rename omits the existing cue position or slot revision then broken
 * - if an agent cannot send the same comment through hot_cue_save then broken
 * - if a hot-cue write stops raising deck pending while it is in flight then broken
 * - if the visible Undo is disabled by that pending write then broken
 * - if tabbing to Cancel then tabbing away without activating it leaves the
 *   draft popover open then broken (PR #1270 carry-over)
 */

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
let ipc;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
});

function source(relativePath) {
	const text = readFileSync(`${SRC}/${relativePath}`, 'utf8');
	assert.ok(text.length > 0, `${relativePath} read as empty`);
	return text;
}

test('an empty-slot click enters focused rename mode before persistence begins', () => {
	const text = source('lib/components/rb/deck/HotCueBank.svelte');
	const saveStart = text.indexOf('async function onSlotClick');
	const saveEnd = text.indexOf('\n\t}', saveStart);
	assert.ok(saveStart >= 0 && saveEnd > saveStart, 'onSlotClick must remain a named testable flow');
	const saveFlow = text.slice(saveStart, saveEnd);

	assert.match(
		saveFlow,
		/await beginRename\(entry\.slot, deck\.position_ms, deck\.stable_id\);/,
		'an empty slot must focus its editor before a persistence round trip can absorb keystrokes'
	);
	assert.match(text, /import\s*\{\s*tick\s*\}\s*from\s*['"]svelte['"]/);
	assert.match(text, /await tick\(\);\s*if \(renameInputEl === null\) throw new Error/);
	assert.match(text, /renameInputEl\.focus\(\)/);
	assert.match(text, /<input[\s\S]*class="[^"]*cue-name"[\s\S]*bind:this=\{renameInputEl\}/);
	assert.match(text, /bind:value=\{renameDraft\}/);
	// bot review P2 (pin c20eeb07cae0 follow-up): onblur now inspects the
	// event before committing, so tabbing to the cancel button does not
	// auto-save a discarded draft - covered in full by
	// hot-cue-edit-and-greedy-columns.test.mjs. This still asserts the
	// blur path DOES commit on every ordinary blur.
	assert.match(text, /void commitRename\(entry\);/);
	assert.match(text, /event\.key === 'Enter'/);
	assert.match(text, /event\.key === 'Escape'/);
});

test('a new cue saves the entered name against its click-time position', () => {
	const text = source('lib/components/rb/deck/HotCueBank.svelte');
	const commitStart = text.indexOf('async function commitRename');
	const commitEnd = text.indexOf('\n\t}', commitStart);
	assert.ok(commitStart >= 0 && commitEnd > commitStart, 'commitRename must remain a named testable flow');
	const commitFlow = text.slice(commitStart, commitEnd);

	assert.match(
		commitFlow,
		/await onSave\(entry\.slot, comment, newCueAtMs, true, stableId\)/,
		'a new cue must persist the complete draft at the position captured when its pad was clicked'
	);
	assert.match(
		source('lib/rb/deck-hot-cue-actions.ts'),
		/async function saveHotCueAt\(\s*slot: HotCueSlot,\s*comment\?: string,\s*fixedPositionMs\?: number,\s*quantizeFixedPosition = false,\s*expectedStableId\?: string\s*\): Promise<HotCueMutation>/,
		'the deck hot-cue action adapter must accept a typed name and captured position'
	);
});

test('a named new cue refreshes Undo with its server reversal token', () => {
	const text = source('lib/components/rb/deck/HotCueBank.svelte');
	const commitStart = text.indexOf('async function commitRename');
	const commitEnd = text.indexOf('\n\t}', commitStart);
	assert.ok(commitStart >= 0 && commitEnd > commitStart, 'commitRename must remain a named testable flow');
	const commitFlow = text.slice(commitStart, commitEnd);

	assert.match(
		commitFlow,
		/setUndo\(entry\.slot, await onSave\(entry\.slot, comment, newCueAtMs, true, stableId\)\);/,
		'the visible Undo action must receive the reversal token from the named create'
	);
});

test('a blur-triggering populated-pad click stays immediate while writes serialize', () => {
	const text = source('lib/components/rb/deck/HotCueBank.svelte');
	assert.match(
		text,
		/const previousCompletion = busySlotCompletion;\s*let release!?: \(\) => void;\s*busySlotCompletion = new Promise/,
		'queued writes must atomically extend one promise tail instead of waking together'
	);
	assert.match(
		text,
		/disabled=\{busySlot === entry\.slot \|\| renameSlot === entry\.slot\}/,
		'a write in one slot must not disable the different button used to leave the input'
	);
	const clickStart = text.indexOf('async function onSlotClick');
	const clickEnd = text.indexOf('\n\t}', clickStart);
	assert.ok(clickStart >= 0 && clickEnd > clickStart, 'onSlotClick must remain a named testable flow');
	const clickFlow = text.slice(clickStart, clickEnd);
	assert.match(clickFlow, /if \(entry\.cue !== null\) \{\s*await onJump\(entry\.slot\);\s*return;/);
	assert.doesNotMatch(clickFlow, /await acquireBusySlot\(entry\.slot\);\s*if \(entry\.cue !== null\)/);
	assert.match(
		source('lib/rb/performance-ipc.svelte.ts'),
		/if \(command\.type === 'hot_cue_save' \|\| command\.type === 'hot_cue_clear' \|\| command\.type === 'hot_cue_restore'\) \{\s*return \[_persistenceScope\(deck\)\];\s*\}/,
		'hot-cue persistence must use its own scope so a blur write cannot queue a Class A seek'
	);
});

test('a pending new-cue edit is bound to the track that opened it', () => {
	const bank = source('lib/components/rb/deck/HotCueBank.svelte');
	const actions = source('lib/rb/deck-hot-cue-actions.ts');
	assert.match(bank, /let renameStableId: string \| null = \$state\(null\);/);
	assert.match(bank, /renameStableId = stableId;/);
	assert.match(actions, /if \(expectedStableId !== undefined && deck\.stable_id !== expectedStableId\) \{/);
	assert.match(actions, /pending edit belongs to \$\{expectedStableId\}, current track is \$\{deck\.stable_id\}/);
});

test('a failed rename preserves its open editor and draft for retry', () => {
	const text = source('lib/components/rb/deck/HotCueBank.svelte');
	const commitStart = text.indexOf('async function commitRename');
	const commitEnd = text.indexOf('\n\t}', commitStart);
	assert.ok(commitStart >= 0 && commitEnd > commitStart, 'commitRename must remain a named testable flow');
	const commitFlow = text.slice(commitStart, commitEnd);
	assert.match(
		commitFlow,
		/setUndo\(entry\.slot, await onSave\(entry\.slot, comment, newCueAtMs, true, stableId\)\);\s*}\s*else if \(entry\.cue !== null\)[\s\S]*if \(renameSlot === entry\.slot\) \{\s*renameSlot = null;/,
		'the editor may close only after its persistence call succeeded'
	);
	assert.match(commitFlow, /const comment = renameDraft\.trim\(\) === '' \? undefined : renameDraft;/);
	assert.match(commitFlow, /if \(renameSlot === entry\.slot\) \{\s*renameSlot = null;/);
});

test('hot_cue_save carries an optional string comment through strict browser IPC', () => {
	const ipc = source('lib/rb/performance-ipc.svelte.ts');
	assert.match(
		ipc,
		/\{ type: 'hot_cue_save'; deck: DeckId; slot: HotCueSlot; in_ms: number; revision: string; comment\?: string \| null \}/,
		'hot_cue_save must expose the optional comment to agent-native callers'
	);
	assert.match(ipc, /_optionalStringOrNull\('comment', record\.comment\)/);
	assert.match(
		ipc,
		/saveHotCue\(\s*stableId, command\.slot, savedPositionMs, command\.revision, command\.comment\s*\)/
	);
});

test('agent-native hot_cue_save accepts names and rejects non-string comments', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'hot_cue_save',
				deck: 1,
				slot: 'A',
				in_ms: 1000,
				revision: 'etag',
				comment: 'Intro'
			}),
			/deck is not loaded/i,
			'a valid cue name must pass IPC parsing and reach the real unloaded-deck guard'
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'hot_cue_save',
				deck: 1,
				slot: 'A',
				in_ms: 1000,
				revision: 'etag',
				comment: 42
			}),
			/comment must be a string or null/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('a hot-cue write raises deck pending before it settles', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const inFlight = window.musicDjToolsPerformance.dispatch({
			type: 'hot_cue_save',
			deck: 1,
			slot: 'A',
			in_ms: 1000,
			revision: 'etag',
			comment: 'Intro'
		});
		assert.ok(
			ipc.performanceCommandStatus.deck_pending[1] > 0,
			'a blur-started hot-cue write raises `pending` synchronously, so any control gated ' +
				'on `pending` goes disabled before the browser delivers the click that caused the blur'
		);
		await assert.rejects(inFlight, /deck is not loaded/i);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('the Undo button survives being the control that triggered the blur', () => {
	const text = source('lib/components/rb/deck/HotCueBank.svelte');
	const undoAt = text.indexOf('class="rb-lit-button undo"');
	assert.notEqual(undoAt, -1, 'if the undo markup moved then this guard is pointed at nothing');
	const undoTagEnd = text.indexOf('>', undoAt);
	assert.notEqual(undoTagEnd, -1, 'unterminated undo button tag');
	const undoTag = text.slice(undoAt, undoTagEnd);

	assert.ok(
		!undoTag.includes('disabled'),
		'clicking Undo to leave the cue-name input blurs it, and that blur starts the write ' +
			'that raises `pending`: a disabled gate on this button eats the very click that ' +
			'opened it, forcing the operator to click Undo a second time'
	);
	assert.ok(
		undoTag.includes('aria-busy={pending}'),
		'dropping the disable must not also drop the in-flight signal it carried'
	);
	const undoStart = text.indexOf('async function undoLastMutation');
	const undoEnd = text.indexOf('\n\t}', undoStart);
	assert.ok(undoStart >= 0 && undoEnd > undoStart, 'undoLastMutation must remain a named testable flow');
	const undoFlow = text.slice(undoStart, undoEnd);
	assert.match(
		undoFlow,
		/const release = await acquireBusySlot\(requestedSlot\);[\s\S]*if \(undo === null\) return;/,
		'without the disable, a repeated Undo must be serialized and then find its token already spent'
	);
});

test('tabbing to Cancel then away without activating it still commits the deferred draft', () => {
	const text = source('lib/components/rb/deck/HotCueBank.svelte');
	const cancelAt = text.indexOf('class="cue-name-cancel"');
	assert.notEqual(cancelAt, -1, 'if the cancel button markup moved then this guard is pointed at nothing');
	// Not indexOf('>', cancelAt): several attributes on this tag are arrow
	// function handlers (`=>`), whose '>' would end the slice early. A
	// fixed window comfortably covering the whole opening tag is simpler
	// and more robust than trying to parse past every `=>` in the markup.
	const cancelTag = text.slice(cancelAt, cancelAt + 1000);

	assert.match(
		cancelTag,
		/onblur=\{\(event\) => \{[\s\S]*?if \(event\.relatedTarget !== renameInputEl\) void commitRename\(entry\);/,
		'the name input defers its commit to this button when Tab is headed here (bot review ' +
			'P2, pin c20eeb07cae0) - if THIS button also lets focus leave without deciding, ' +
			'the deferred commit is dropped and the popover is left open forever'
	);
});
