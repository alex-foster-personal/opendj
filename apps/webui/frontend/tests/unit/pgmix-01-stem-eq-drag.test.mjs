// requirement: PGMIX-7692
// Stem EQ dials follow a drag live (Preview 5854e44a55, pin 989f4f236f90).
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

globalThis.location ??= { search: '' };

let ipc;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
});

test('stem_gain and stem_eq_mode skip the deck transport queue, like EQ', () => {
	assert.equal(
		ipc.performanceCommandQueueScopes({ type: 'stem_gain', deck: 2, stem: 'vocal', value: 0.7 }),
		null
	);
	assert.equal(
		ipc.performanceCommandQueueScopes({ type: 'stem_eq_mode', deck: 2, enabled: true }),
		null
	);
	// Control: a transport command still waits on its deck's queue.
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'pitch_range', deck: 2, range: 8 }),
		[2]
	);
});

test('a lone dial shows the pointer while the drag is down, then the committed value', () => {
	const src = readFrontendSource('src/lib/components/rb/mixer/Knob.svelte');
	assert.match(src, /const displayValue = \$derived\(dragShown \?\? value\);/);
	assert.match(src, /const angleDeg = \$derived\(\(displayValue - 0\.5\) \* SWEEP_DEG\);/);
	assert.match(
		src,
		/dragShown = selectionDrag === null && dragPartnerId === null \? value : null;/
	);
	assert.match(
		src,
		/const target = clamp01\(dragStartValue \+ singleDragDelta\(dragSumDx, dragSumDy\)\);/
	);
	assert.match(src, /if \(dragPartnerId === null\) dragShown = target;/);
	assert.match(src, /window\.addEventListener\('pointerup', handlePointerUp\)/);
	assert.match(src, /window\.addEventListener\('pointercancel', handlePointerCancel\)/);
	assert.match(src, /aria-valuenow=\{displayValue\}/);
});

test('the stem dial writes its gain in the pointer turn and the engine echoes stem acks', () => {
	const strip = readFrontendSource('src/lib/components/rb/mixer/ChannelStrip.svelte');
	assert.match(strip, /deck\.stems\.controls\[stem\]\.gain = value;\s*onStemGain\(stem, value\);/);
	const rust = readFrontendSource('src/lib/audio-engine/rust-engine.ts');
	assert.match(
		rust,
		/case 'stem_gain':\s*deckStates\[command\.deck\]\.stems\.controls\[command\.stem\]\.gain = command\.value;/
	);
	assert.match(
		rust,
		/case 'stem_eq_mode':\s*mixerState\.channels\[command\.deck\]\.stem_eq_mode = command\.enabled;/
	);
});

// Codex P2 4404898456 on #7692: an Alt-linked drag commits
// through applyLinkedDelta with the stagger added, so a pointer preview would
// put the needle and aria-valuenow on a value the gain never takes.
test('a linked drag never previews the pointer; it shows the committed value', () => {
	const src = readFrontendSource('src/lib/components/rb/mixer/Knob.svelte');
	// Count preview writes only inside handlePointerMove (stop before window-listener teardown).
	const moveStart = src.indexOf('function handlePointerMove');
	assert.notEqual(moveStart, -1, 'handlePointerMove found');
	// Slice the move handler only: code after it (teardown, finishPointerDrag) also assigns dragShown.
	const moveEnd = src.indexOf('function removeWindowDragListeners', moveStart);
	assert.notEqual(moveEnd, -1, 'finishPointerDrag follows move');
	const move = src.slice(moveStart, moveEnd);
	const writes = move.match(/dragShown = [^;]+;/g) ?? [];
	assert.equal(writes.length, 1, 'one preview write in the move handler');
	const guarded = /if \(dragPartnerId === null\) dragShown = target;/;
	assert.match(move, guarded, 'the move preview is guarded on no linked partner');
	// Control: a lone dial still previews (the pointer-down rule) so the guard
	// is not an overshoot that drops the preview for every dial.
	assert.match(
		src,
		/dragShown = selectionDrag === null && dragPartnerId === null \? value : null;/
	);
});
