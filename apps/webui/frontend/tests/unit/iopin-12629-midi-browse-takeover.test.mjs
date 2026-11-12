// requirement: IOPIN-12629
import assert from 'node:assert/strict';
import { before, describe, it, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let browseAdapter;
let browserNav;
let browserMidi;
let browseScroll;
let takeoverPolicy;

before(async () => {
	[browseAdapter, browserNav, browserMidi, browseScroll, takeoverPolicy] = await Promise.all([
		loadTypeScriptModule('src/lib/rb/midi/browse-adapter.ts'),
		loadTypeScriptModule('src/lib/rb/browser-navigation.ts'),
		loadTypeScriptModule('src/lib/components/rb/browser/browser-midi-selection.ts'),
		loadTypeScriptModule('src/lib/components/rb/browser/browse-scroll.ts'),
		loadTypeScriptModule('src/lib/rb/midi/takeover-policy.ts')
	]);
});

const rows = [
	{ stable_id: 'a', order: 0 },
	{ stable_id: 'b', order: 1 },
	{ stable_id: 'c', order: 2 }
];

test('IOPIN-12629: browse wheel advances selection when no context menu owns navigation', () => {
	// [if] encoder turns with no menu open [then] selection moves, [else stop].
	globalThis.document = {
		querySelector: () => null
	};
	assert.equal(browseAdapter.browseWheelMayAdvance(), true);
	const next = browserMidi.nextMidiSelection(rows, 'a', 1);
	assert.equal(next?.stable_id, 'b');
	delete globalThis.document;
});

test('IOPIN-12629: open context menu blocks browse wheel selection steps', () => {
	// [if] a context menu is open [then] browse wheel must not move rows, [else stop].
	globalThis.document = {
		querySelector: (sel) => (sel === '[data-testid="context-menu"]' ? {} : null)
	};
	assert.equal(browseAdapter.browseWheelMayAdvance(), false);
	assert.equal(browserMidi.nextMidiSelection(rows, 'a', 1), null);
	delete globalThis.document;
});

test('IOPIN-12629: directional browse scroll places the selected row ahead in the viewport', () => {
	// [if] MIDI or keyboard moves selection [then] scroll targets look-ahead, [else stop].
	const many = Array.from({ length: 40 }, (_, order) => ({ order }));
	const targetDown = browseScroll.browseScrollTarget({
		request: { order: 10, direction: 1, revision: 1 },
		rows: many,
		viewportHeight: 200,
		rowHeight: 22,
		headerOffsetPx: 20,
		slotOfRow: (index) => index
	});
	const targetUp = browseScroll.browseScrollTarget({
		request: { order: 10, direction: -1, revision: 2 },
		rows: many,
		viewportHeight: 200,
		rowHeight: 22,
		headerOffsetPx: 20,
		slotOfRow: (index) => index
	});
	assert.ok(targetDown !== null && targetUp !== null);
	assert.notEqual(targetDown, targetUp, 'direction chooses different look-ahead offsets');
	const downOffset = 200 * 0.35;
	const upOffset = 200 * 0.65;
	assert.ok(
		Math.abs(targetDown - (10 * 22 + 20 - downOffset)) < 2,
		'down scroll uses the 35% lookahead'
	);
	assert.ok(Math.abs(targetUp - (10 * 22 + 20 - upOffset)) < 2, 'up scroll uses the 65% lookahead');
});

describe('IOPIN-12629 library focus walk', () => {
	it('walks playlist to tracks to deck with left and right', () => {
		// [if] Left then Right twice [then] focus walks playlist tracks deck, [else stop].
		assert.equal(browserNav.moveBrowserFocus('tracks', 'ArrowLeft'), 'playlist');
		assert.equal(browserNav.moveBrowserFocus('playlist', 'ArrowRight'), 'tracks');
		assert.equal(browserNav.moveBrowserFocus('tracks', 'ArrowRight'), 'deck');
	});

	it('maps WASD vertical keys to browse deltas', () => {
		assert.equal(browserNav.browserSelectionDelta('w'), -1);
		assert.equal(browserNav.browserSelectionDelta('S'), 1);
	});
});

test('IOPIN-12629: pickup ghost blocks jump until hardware crosses software', () => {
	// [if] software moved a mapped fader in pickup [then] ghost shows and holds, [else stop].
	const crossfader = { type: 'mixer_global', target: 'crossfader' };
	const identity = takeoverPolicy.makeTakeoverIdentity('dev-a', 'cc:1:7', crossfader);
	assert.ok(identity);
	const policy = new takeoverPolicy.AbsoluteTakeoverPolicy();
	assert.equal(
		policy.observeAbsolute({ identity, hardwareValue: 0.5, softwareValue: 0.5 }).apply,
		true
	);
	policy.noteSoftwareValue('mixer:global:crossfader', 0.8);
	assert.deepEqual(policy.ghostForFunction('mixer:global:crossfader'), { value: 0.5, target: 0.8 });
	const held = policy.observeAbsolute({
		identity,
		hardwareValue: 0.2,
		softwareValue: 0.8
	});
	assert.equal(held.apply, false);
	assert.deepEqual(policy.ghostForFunction('mixer:global:crossfader'), { value: 0.2, target: 0.8 });
});

test('IOPIN-12629: jump mode applies the next absolute tick immediately', () => {
	// [if] Jump is selected [then] the next absolute tick applies, [else stop].
	const trim = { type: 'mixer_channel', deck: 1, target: 'trim' };
	const identity = takeoverPolicy.makeTakeoverIdentity('dev-b', 'cc:2:1', trim);
	const policy = new takeoverPolicy.AbsoluteTakeoverPolicy('jump');
	assert.equal(
		policy.observeAbsolute({ identity, hardwareValue: 0.1, softwareValue: 0.75 }).apply,
		true
	);
});
