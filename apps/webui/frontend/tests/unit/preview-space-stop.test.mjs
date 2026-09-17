/**
 * CUEOUT-15 R5: Space stops a sounding preview first, then is released.
 *
 * Regression lines:
 * - if Space toggles a deck while a preview sounds then stopping a preview in the booth starts or stops the room - broken
 * - if the stopping Space press is not consumed then a focused button activates as well - broken
 * - if a held Space auto-repeats through after the stop then a deck toggles on the repeat - broken
 * - if Space is touched with no preview playing then deck play/pause stops working - broken
 * - if Space after release is still swallowed then transport is dead until reload - broken
 * - if Space in a text field stops the preview then typing a search query kills the preview and eats the space - broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/player/preview-space-stop.ts');
});

function world({ playing = true } = {}) {
	const state = { playing, stops: 0 };
	const handler = mod.createPreviewSpaceStop({
		isPreviewPlaying: () => state.playing,
		stopPreview: () => {
			state.stops += 1;
			state.playing = false;
		}
	});
	return { state, handler };
}

const CHROME = { tagName: 'DIV', getAttribute: () => null };

function key(target = CHROME, overrides = {}) {
	return {
		code: 'Space',
		key: ' ',
		target,
		defaultPrevented: false,
		propagationStopped: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopImmediatePropagation() {
			this.propagationStopped = true;
		},
		...overrides
	};
}

const consumed = (ev) => ev.defaultPrevented && ev.propagationStopped;

test('Space while a preview sounds stops it and is consumed before the deck toggle sees it', () => {
	const { state, handler } = world();
	const down = key();
	handler.onKeyDown(down);
	assert.equal(state.stops, 1, 'if Space does not stop the preview then the shortcut is dead - broken');
	assert.ok(
		consumed(down),
		'if Space toggles a deck while a preview sounds then stopping a preview in the booth starts or stops the room - broken'
	);
});

test('a focused button does not activate from the stopping press (keyup swallowed too)', () => {
	const { handler } = world();
	const button = { tagName: 'BUTTON', getAttribute: () => null };
	const down = key(button);
	handler.onKeyDown(down);
	const up = key(button);
	handler.onKeyUp(up);
	assert.ok(
		consumed(down) && consumed(up),
		'if the stopping Space press is not consumed then a focused button activates as well - broken'
	);
});

test('held Space auto-repeat is swallowed until release, then Space is normal again', () => {
	const { state, handler } = world();
	handler.onKeyDown(key());
	const repeat = key(CHROME, { repeat: true });
	handler.onKeyDown(repeat);
	assert.ok(
		consumed(repeat),
		'if a held Space auto-repeats through after the stop then a deck toggles on the repeat - broken'
	);
	assert.equal(state.stops, 1);

	handler.onKeyUp(key());
	const next = key();
	handler.onKeyDown(next);
	assert.ok(
		!consumed(next),
		'if Space after release is still swallowed then transport is dead until reload - broken'
	);
	const nextUp = key();
	handler.onKeyUp(nextUp);
	assert.ok(!consumed(nextUp));
});

test('with no preview playing Space is untouched', () => {
	const { state, handler } = world({ playing: false });
	const down = key();
	handler.onKeyDown(down);
	handler.onKeyUp(key());
	assert.equal(state.stops, 0);
	assert.ok(
		!down.defaultPrevented && !down.propagationStopped,
		'if Space is touched with no preview playing then deck play/pause stops working - broken'
	);
});

test('typing a space in a text field never stops the preview', () => {
	for (const target of [
		{ tagName: 'INPUT', type: 'search', getAttribute: () => null },
		{ tagName: 'INPUT', type: '', getAttribute: () => null },
		{ tagName: 'TEXTAREA', getAttribute: () => null },
		{ tagName: 'DIV', isContentEditable: true, getAttribute: () => null },
		{ tagName: 'DIV', getAttribute: (n) => (n === 'role' ? 'searchbox' : null) }
	]) {
		const { state, handler } = world();
		const down = key(target);
		handler.onKeyDown(down);
		assert.equal(
			state.stops,
			0,
			`if Space in a text field stops the preview then typing a search query kills the preview and eats the space - broken (${target.tagName} ${target.type ?? ''})`
		);
		assert.ok(!consumed(down));
	}
});

test('a checkbox or range input is not text entry, so Space still stops the preview', () => {
	assert.equal(
		mod.isTextEntryTarget({ tagName: 'INPUT', type: 'checkbox', getAttribute: () => null }),
		false
	);
	assert.equal(
		mod.isTextEntryTarget({ tagName: 'INPUT', type: 'range', getAttribute: () => null }),
		false
	);
});

test('other keys are ignored', () => {
	const { state, handler } = world();
	const enter = key(CHROME, { code: 'Enter', key: 'Enter' });
	handler.onKeyDown(enter);
	assert.equal(state.stops, 0);
	assert.ok(!consumed(enter));
});
