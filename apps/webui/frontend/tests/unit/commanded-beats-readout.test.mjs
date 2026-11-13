/**
 * D4 (#9455): a selected Knob shows the commanded move length as a small `4b` readout,
 * rendered for real through svelte's SSR renderer.
 *
 * Regression lines:
 * - if the length changes and the readout on the selected knob does not show the new value then broken
 * - if an unselected knob shows a length readout then broken
 * - if the readout is not aria-hidden art then broken
 */
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const SSR_ENTRY = [
	"export { default as Knob } from '$lib/components/rb/mixer/Knob.svelte';",
	"export { _resetKnobRegistryForTests, clearKnobSelection, commandedKeyHost, registerKnob, setKnobSelection } from '$lib/rb/knob-control.svelte';",
	"export { runCommand } from '$lib/keyboard/command-registry';",
	"export { render } from 'svelte/server';"
].join('\n');

let ssr;

before(async () => {
	globalThis.requestAnimationFrame ??= () => 0;
	globalThis.cancelAnimationFrame ??= () => {};
	ssr = await loadSvelteSsrModule(SSR_ENTRY);
});

beforeEach(() => {
	ssr._resetKnobRegistryForTests();
	ssr.clearKnobSelection();
	ssr.commandedKeyHost().setBeats(4);
	for (const id of ['1:low', '1:mid']) {
		let v = 0.5;
		ssr.registerKnob({ id, getValue: () => v, setValue: (x) => (v = x) });
	}
});

function renderKnob(knobId) {
	return ssr.render(ssr.Knob, {
		props: { knobId, label: knobId, value: 0.5, onchange: () => {} }
	}).body;
}

function readout(html, knobId) {
	const match = html.match(
		new RegExp(`<span[^>]*data-testid="commanded-beats-${knobId}"[^>]*>([^<]*)</span>`)
	);
	return match === null ? null : { tag: match[0], text: match[1] };
}

test('if the length changes then the readout on the selected knob shows the new value', () => {
	/** [if] 1:low is selected and = then - - run [then] its readout reads 4b, 8b, then 2b, [else stop]. */
	ssr.setKnobSelection(['1:low']);
	assert.equal(readout(renderKnob('1:low'), '1:low').text, '4b');
	const run = (id) => ssr.runCommand(id, { timeStamp: 0, commanded: ssr.commandedKeyHost() });
	run('dial.duration.longer');
	assert.equal(readout(renderKnob('1:low'), '1:low').text, '8b');
	run('dial.duration.shorter');
	run('dial.duration.shorter');
	assert.equal(readout(renderKnob('1:low'), '1:low').text, '2b');
});

test('only the selected knob shows the readout, and it is aria-hidden art', () => {
	/** [if] 1:low is selected and 1:mid is not [then] only 1:low renders an aria-hidden readout, [else stop]. */
	ssr.setKnobSelection(['1:low']);
	assert.match(readout(renderKnob('1:low'), '1:low').tag, /aria-hidden="true"/);
	assert.equal(readout(renderKnob('1:mid'), '1:mid'), null);
	ssr.clearKnobSelection();
	assert.equal(readout(renderKnob('1:low'), '1:low'), null);
});
