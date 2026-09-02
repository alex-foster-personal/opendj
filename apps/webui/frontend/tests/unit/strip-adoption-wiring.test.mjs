import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// BrowserPanel's decoded-strip adoption effect must watch every id a pane
// currently shows as selected, not only its singular last-clicked anchor.
// BrowserPanel cannot be mounted in this node harness (see
// library-row-hydration.test.mjs), so this asserts against the component
// source, the same pattern that file already established.
//
// Regression line:
// - if the effect gathers ids from pane.selected_id alone then a Cmd/Ctrl
//   multi-selected row that isn't the most-recently-clicked one never adopts
//   its own completed decode (discussion_r3910053530)

const BROWSER_PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);

/** The strip-adoption effect body, located by its distinctive call to the
 * pure cross-pane writer immediately downstream of the id-gathering loop. */
function stripAdoptionEffectBody() {
	const source = readFileSync(BROWSER_PANEL, 'utf8');
	const anchor = 'applyDecodedStripAcrossPanes(';
	const anchorAt = source.indexOf(anchor);
	assert.ok(anchorAt >= 0, `${anchor} not found in BrowserPanel.svelte`);
	const effectStart = source.lastIndexOf('$effect(() => {', anchorAt);
	assert.ok(effectStart >= 0, 'enclosing $effect not found before the applyDecodedStripAcrossPanes call');
	const effectEnd = source.indexOf('\n\t});', anchorAt);
	assert.ok(effectEnd > anchorAt, 'enclosing $effect close not found after the applyDecodedStripAcrossPanes call');
	return source.slice(effectStart, effectEnd);
}

test('the strip-adoption effect unions each pane\'s full selected_ids, not only selected_id', () => {
	const body = stripAdoptionEffectBody();
	assert.match(
		body,
		/for\s*\(\s*const\s+\w+\s+of\s+p\.selected_ids\s*\)/,
		'the effect no longer iterates p.selected_ids - an earlier multi-selected row (Cmd/Ctrl-click) ' +
			'whose id is not the current selected_id anchor would stop adopting its own decode'
	);
	assert.match(
		body,
		/p\.selected_id\s*!==\s*null/,
		'the effect stopped watching p.selected_id - a single-select pane with no multi-selection would ' +
			'stop adopting its own decode'
	);
});
