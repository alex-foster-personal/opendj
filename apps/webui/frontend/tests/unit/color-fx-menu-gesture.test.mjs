// requirement: COLORFX-02
// requirement: COLORFX-10
// requirement: COLORFX-13
// requirement: COLORFX-8341
// requirement: COLORFX-10320
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
const MIXER = readFrontendSource('src/lib/components/rb/Mixer.svelte');

function divisionGroup() {
	const start = STRIP.indexOf('{#if showsEchoDivisions(colorSettings.effect)}');
	assert.ok(start >= 0, 'the division group is gated on ECHO (control)');
	const end = STRIP.indexOf('{/if}', start);
	return STRIP.slice(start, end);
}

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

test('the menu offers the shipped effect list, one option per effect, dispatching color_fx', () => {
	const menu = STRIP.slice(STRIP.indexOf('class="color-fx-menu"'), STRIP.indexOf('{/each}', STRIP.indexOf('class="color-fx-menu"')));
	assert.ok(menu.length > 0, 'the menu markup is present (control)');
	assert.match(menu, /role="menu"/);
	assert.match(menu, /aria-label=\{`color FX type deck \$\{deckId\}`\}/);
	assert.match(menu, /\{#each COLOR_FX_EFFECTS as effect \(effect\)\}/);
	assert.match(menu, /role="menuitemradio"/);
	assert.match(menu, /aria-checked=\{colorSettings\.effect === effect\}/);
	assert.match(menu, /onclick=\{\(\) => chooseColorFx\(effect\)\}/);
	assert.match(STRIP, /if \(effect !== colorSettings\.effect\) oncolorfx\(effect\);/);
});

test('every menu option is inert by name in Rust engine mode', () => {
	const option = STRIP.slice(STRIP.indexOf('role="menuitemradio"'), STRIP.indexOf('</button', STRIP.indexOf('role="menuitemradio"')));
	assert.ok(option.length > 0, 'the option is present (control)');
	assert.match(option, /data-rust-command="color_fx"/);
});

test('a SPACE or ECHO channel holds its color knob inert in Rust mode; FILTER stays live', () => {
	assert.match(STRIP, /class="filter-knob"\s+data-rust-command=\{colorSettings\.effect === 'filter' \? undefined : 'color_fx'\}/);
	// STEMDIAL-DRAG-01: never the slot, which also holds the DRUMS stem dial.
	assert.doesNotMatch(STRIP, /class="filter-slot"\s+data-rust-command=/);
});

test('the caption follows the effect and non-FILTER effects are tinted', () => {
	assert.match(STRIP, /label=\{COLOR_FX_LABELS\[colorSettings\.effect\]\}/);
	assert.doesNotMatch(STRIP, /label="FILTER"/, 'the caption is no longer hard-coded');
	assert.match(STRIP, /filter: undefined,/);
	assert.match(STRIP, /\{\.\.\.\(colorTint \? \{ accentColor: colorTint \} : \{\}\)\}/);
});

test('the echo title names its numbers on the chevron and the menu', () => {
	assert.equal(STRIP.match(/title=\{colorFxTitle\}/g)?.length, 2);
	assert.match(STRIP, /beat at \$\{bpm\.toFixed\(1\)\} BPM = \$\{ms\} ms per repeat/);
});

test('COLORFX-10320: the menu renders the divisions as checked menuitemradio items held inert by Rust', () => {
	const group = divisionGroup();
	assert.match(group, /\{#each COLOR_FX_DIVISIONS as beats \(beats\)\}/);
	assert.match(group, /role="menuitemradio"/);
	assert.match(group, /aria-checked=\{colorSettings\.echo_beats === beats\}/);
	assert.match(group, /title=\{echoDivisionTitle\(beats, colorSettings\.tempo_bpm\)\}/);
	assert.match(group, /data-rust-command="color_fx"/);
	assert.match(group, /onclick=\{\(\) => chooseEchoDivision\(beats\)\}/);
	const menu = STRIP.indexOf('class="color-fx-menu"');
	const effects = STRIP.indexOf('{#each COLOR_FX_EFFECTS as effect (effect)}', menu);
	assert.ok(
		menu >= 0 && effects > menu && STRIP.indexOf('{#if showsEchoDivisions', effects) > effects
	);
});

test('COLORFX-10320: a pick dispatches color_fx echo_beats only, never the knob value', () => {
	assert.match(STRIP, /if \(beats !== colorSettings\.echo_beats\) onechodivision\(beats\);/);
	assert.match(MIXER, /onechodivision=\{\(beats\) => handleEchoDivision\(deck, beats\)\}/);
	const handler = MIXER.slice(
		MIXER.indexOf('function handleEchoDivision'),
		MIXER.indexOf('function handleFader')
	);
	assert.match(handler, /runPerformanceCommandFromUi\(\{ type: 'color_fx', deck, echo_beats \}\)/);
	assert.doesNotMatch(handler, /value/, 'the division command carries no knob value');
});

test('the mixer dispatches color_fx through the typed dispatcher for that deck', () => {
	assert.match(MIXER, /runPerformanceCommandFromUi\(\{ type: 'color_fx', deck, effect \}\)/);
	assert.match(MIXER, /oncolorfx=\{\(effect\) => handleColorFx\(deck, effect\)\}/);
});

test('COLORFX-8341: a Cmd-click anywhere on the color slot cycles, in capture, before the knob or select see it', () => {
	const slot = STRIP.slice(STRIP.indexOf('class="filter-slot"'), STRIP.indexOf('<Knob', STRIP.indexOf('class="filter-slot"')));
	assert.match(slot, /onpointerdowncapture=\{handleColorSlotPointerDown\}/);
	assert.match(slot, /onmousedowncapture=\{handleColorSlotMouseDown\}/);
	assert.match(slot, /onclickcapture=\{handleColorSlotClick\}/);
	const click = STRIP.slice(STRIP.indexOf('function handleColorSlotClick'), STRIP.indexOf('function handleColorSlotClick') + 400);
	assert.match(click, /isColorFxCycleGesture\(event, cycleModifier\)/);
	assert.match(click, /event\.stopPropagation\(\)/, 'not also a deck select');
	assert.match(click, /oncolorfx\(COLOR_FX_NEXT\)/);
	for (const name of ['handleColorSlotPointerDown', 'handleColorSlotMouseDown']) {
		const body = STRIP.slice(STRIP.indexOf(`function ${name}`), STRIP.indexOf(`function ${name}`) + 300);
		assert.match(body, /event\.preventDefault\(\);[\s\S]*event\.stopPropagation\(\)/, `${name} swallows the press`);
	}
	assert.match(STRIP, /const cycleModifier = quickPickModifier\(/);
});

test('COLORFX-13: ChannelStrip and Mixer wire All channels and deck all dispatch', () => {
	const effectsEnd = STRIP.indexOf('{/each}', STRIP.indexOf('{#each COLOR_FX_EFFECTS'));
	const allBtn = STRIP.indexOf('class="color-fx-option color-fx-all"', effectsEnd);
	const divisions = STRIP.indexOf('showsEchoDivisions', effectsEnd);
	assert.ok(allBtn >= 0 && allBtn < divisions, 'All channels sits after effects, before divisions');
	const snippet = STRIP.slice(allBtn - 80, allBtn + 320);
	assert.match(snippet, /role="menuitem"/);
	assert.match(snippet, /data-rust-command="color_fx"/);
	assert.match(snippet, /All channels/);
	assert.match(snippet, /onclick=\{chooseColorFxAll\}/);
	assert.match(MIXER, /deck: 'all'/);
	assert.match(MIXER, /oncolorfxall=/);
});
