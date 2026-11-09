// requirement: COLORFX-10
// the maintainer, Tue 3 Nov 2026, verbatim: "overlap issue. Filter - also single click on the dial or the label
// should open the options I think. Also sometimes the click drag doesn't catch the first click as the
// dropdown hasn't closed yet - shouldn't block it."
// [if] the color knob is tapped (press + release inside the click slop) [then] the menu opens
// [if] the color knob is tapped while the menu is open [then] it closes and the release does not reopen it
// [if] the menu is open and another knob gets a pointerdown [then] the menu closes AND the press is not consumed
// [if] a pointerdown lands inside the menu [then] the menu stays open for the option click
// [if] Enter / Space activates the knob (no pointer press) [then] the menu toggles
// [if] a caption is wider than its knob column at 8px [then] it drops to 7px; one that fits keeps 8px
// [if] Knob or ChannelStrip regress (deferred tap, preventDefault on the outside press, absolute chevron) [then] ⛔️
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadRuneModule } from './load-rune-module.mjs';

let menu;
let fit;

before(async () => {
	menu = await loadRuneModule(`export * from '$lib/rb/color-fx-menu';`);
	fit = await loadRuneModule(`export * from '$lib/rb/knob-caption-fit';`);
});

const KNOB = readFrontendSource('src/lib/components/rb/mixer/Knob.svelte');
const STRIP = readFrontendSource('src/lib/components/rb/mixer/ChannelStrip.svelte');

function press(state, where) {
	return menu.colorFxMenuOnPointerDown(state, where);
}

test('if a tap on the closed color knob does not open the menu then broken', () => {
	const after = menu.colorFxMenuOnTap(press(menu.COLOR_FX_MENU_CLOSED, 'trigger'));
	assert.equal(after.open, true);
});

test('if a tap on the open color knob reopens the menu on release then broken', () => {
	const open = menu.colorFxMenuOnTap(press(menu.COLOR_FX_MENU_CLOSED, 'trigger'));
	const pressed = press(open, 'trigger');
	assert.equal(pressed.open, false, 'the press itself closes the menu');
	assert.equal(menu.colorFxMenuOnTap(pressed).open, false, 'the release does not reopen it');
	assert.equal(menu.colorFxMenuOnTap(press(menu.colorFxMenuOnTap(pressed), 'trigger')).open, true, 'the next tap opens again');
});

test('if a drag on the color knob that closed the menu leaves the next tap swallowed then broken', () => {
	const open = menu.colorFxMenuOnTap(press(menu.COLOR_FX_MENU_CLOSED, 'trigger'));
	const draggedClosed = press(open, 'trigger'); // drag: no tap follows
	assert.equal(menu.colorFxMenuOnTap(press(draggedClosed, 'trigger')).open, true);
});

test('if a pointerdown on another knob does not close the open menu then broken', () => {
	const open = menu.colorFxMenuOnTap(press(menu.COLOR_FX_MENU_CLOSED, 'trigger'));
	assert.equal(press(open, 'outside').open, false);
});

test('if a pointerdown inside the menu closes it before the option click then broken', () => {
	const open = menu.colorFxMenuOnTap(press(menu.COLOR_FX_MENU_CLOSED, 'trigger'));
	assert.equal(press(open, 'menu').open, true);
	assert.equal(menu.colorFxMenuClose().open, false, 'choosing an option closes it');
});

test('if Enter / Space on the knob does not toggle the menu then broken', () => {
	const opened = menu.colorFxMenuOnTap(menu.COLOR_FX_MENU_CLOSED);
	assert.equal(opened.open, true);
	assert.equal(menu.colorFxMenuOnTap(opened).open, false);
});

test('if the press classifier mislabels menu, trigger or outside targets then broken', () => {
	const item = {};
	const dial = {};
	const other = {};
	const menuRoot = { contains: (n) => n === item };
	const triggerRoot = { contains: (n) => n === dial };
	assert.equal(menu.classifyColorFxPress(item, menuRoot, triggerRoot), 'menu');
	assert.equal(menu.classifyColorFxPress(dial, menuRoot, triggerRoot), 'trigger');
	assert.equal(menu.classifyColorFxPress(other, menuRoot, triggerRoot), 'outside');
	assert.equal(menu.classifyColorFxPress(other, null, triggerRoot), 'outside', 'a closed menu has no root');
});

test('if the outside-press handler can swallow the event then broken', () => {
	const handler = STRIP.slice(STRIP.indexOf('function handleDocumentPointerDown'), STRIP.indexOf('function handleColorFxTap'));
	assert.ok(handler.length > 0, 'the handler is present (control)');
	assert.doesNotMatch(handler, /preventDefault|stopPropagation|stopImmediatePropagation/);
	assert.match(STRIP, /document\.addEventListener\('pointerdown', handleDocumentPointerDown, true\)/, 'capture phase, read-only');
	assert.doesNotMatch(STRIP, /<select/, 'no native select: its popup eats the closing click');
});

test('if the color knob tap is deferred or a drag stops turning it then broken', () => {
	assert.match(STRIP, /ontap=\{handleColorFxTap\}/);
	assert.match(STRIP, /onchange=\{onfilter\}/, 'the knob still turns on drag');
	assert.match(KNOB, /onsingleclick !== undefined \|\| ontap !== undefined \|\| altPressInGroup\) && insideSlop\) return;/, 'jitter inside the slop stays a tap');
	const finish = KNOB.slice(
		KNOB.indexOf('function finishPointerDrag'),
		KNOB.indexOf('function handleDblClick')
	);
	assert.match(
		finish,
		/if \(!live \|\| wasDrag\) return;\n\t\tontap\?\.\(\);/,
		'the tap fires on release, not after the double-click delay'
	);
});

test('if the chevron hangs outside the caption box again then broken', () => {
	assert.doesNotMatch(STRIP, /right: -9px/);
	assert.match(STRIP, /captionTrail=\{colorFxChevron\}/);
	assert.match(KNOB, /\{#if captionTrail\}\{@render captionTrail\(\)\}\{\/if\}<\/span>/, 'the trail renders inside .label');
	assert.match(KNOB, /max-width: var\(--knob-caption-max\);/);
	assert.match(KNOB, /text-overflow: ellipsis;/);
});

test('if a caption that fits loses its 8px size, or one that overflows keeps it, then broken', () => {
	const { fitKnobCaption, KNOB_CAPTION_CFG } = fit;
	assert.equal(KNOB_CAPTION_CFG.FULL_FONT_PX, 8);
	assert.deepEqual(fitKnobCaption('DRUMS', 30), { maxWidthPx: 48, fontPx: 8 });
	assert.deepEqual(fitKnobCaption('TRIM', 24), { maxWidthPx: 42, fontPx: 8 });
	assert.deepEqual(fitKnobCaption('FILTER', 24, 9), { maxWidthPx: 42, fontPx: 8 }, 'MORE: FILTER + chevron fits at 8px');
	assert.deepEqual(fitKnobCaption('FILTER', 18, 9), { maxWidthPx: 36, fontPx: 7 }, 'LESS: FILTER + chevron shrinks to 7px');
	assert.throws(() => fitKnobCaption('X', 0), /positive finite/);
	assert.throws(() => fitKnobCaption('X', 18, -1), /trail width/);
});
