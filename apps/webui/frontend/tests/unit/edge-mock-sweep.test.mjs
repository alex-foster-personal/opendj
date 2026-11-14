// requirement: EDGESAMP-14586, EDGESAMP-14600
/**
 * Issue #10784 / #10785 (F21): the mock parity sweep. The rows that still differed from the
 * approved mock ((private path), variant D)
 * at the head of the stack now render the mock's structure, labels and tokens.
 *
 * Regression lines:
 * - if the caret opens the library panel instead of "Compatible with master" then the top tab stops looking like the mock
 * - if the deck's drag chip loses "Deck N loop" or the "drag to an edge" tag then nobody knows where to drag it
 * - if the sampler's keyboard ring is not the mock's --warn then focus looks like the rest of the app, not the mock
 * - if the CUE-only deck wave fills like a tab wave then the edit modal's wave is taller than the mock's
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

let ssr;
before(async () => {
	ssr = await loadSvelteSsrModule(
		[
			"export { default as EdgeCompatPop } from '$lib/components/rb/sampler/EdgeCompatPop.svelte';",
			"export { default as DeckLoopDragHandle } from '$lib/components/rb/sampler/DeckLoopDragHandle.svelte';",
			"export { default as EdgeWave } from '$lib/components/rb/sampler/EdgeWave.svelte';",
			"export * as draw from '$lib/components/rb/sampler/region-wave-draw';",
			"export { DEFAULT_WAVE } from '$lib/player/sampler/edge-wave';",
			"export { CAMELOT_RULES } from '$lib/player/key/camelot-rules.generated';",
			"export { render } from 'svelte/server';"
		].join('\n'),
		{ css: 'injected' }
	);
});

/** Markup and all scoped CSS of one SSR render. */
function renderWith(component, props) {
	const { head, body } = ssr.render(component, { props });
	const css = [...head.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
	return { body, css };
}

const rule = (css, sel, decl) =>
	assert.match(css, new RegExp(`${sel}[^{]*\\{[^}]*${decl}`), `${sel} { ${decl} }`);

test('the caret popover is the mock compatible pop, beside the top tab', () => {
	/** [if] the R compatible popover renders [then] it has the mock's title, rows and labels in order, 330 px, 486 px off the edge, [else stop]. */
	const { body, css } = renderWith(ssr.EdgeCompatPop, { edge: 'R', onclose: () => undefined });
	assert.match(body, /role="dialog"[^>]*aria-label="Compatible with master"/);
	assert.match(body, /style="top: 8px; right: 486px;"/);
	const order = [
		/<h3[^>]*>Compatible with master</,
		/class="lbl[^"]*">Mode</,
		/>Transition \(T\)</,
		/>Mashup \(M\)</,
		/class="lbl[^"]*">Camelot rules</,
		/class="rules/,
		/BPM within \+-/,
		/aria-label="BPM window"/,
		/half \/ double/,
		/class="lbl[^"]*">BPM direction</,
		/>Both</,
		/>Same</,
		/class="lbl[^"]*">Key shift allowed \(samples play shifted\)</,
		/class="sum/
	];
	const pos = order.map((re) => body.search(re));
	assert.ok(
		pos.every((p) => p >= 0),
		`all present: ${pos}`
	);
	assert.deepEqual(
		[...pos].sort((a, b) => a - b),
		pos,
		'mock DOM order'
	);
	assert.equal(body.match(/class="ck[^"]*"/g).length, ssr.CAMELOT_RULES.length + 2);
	assert.doesNotMatch(body, /compat-filter-panel/);
	rule(css, '\\.pop', 'width: ?330px');
	rule(css, '\\.pop', 'border-radius: ?7px');
	rule(css, '\\.pop', 'background: ?var\\(--es-panel\\)');
	rule(css, '\\.rules', 'grid-template-columns: ?1fr 1fr');
	rule(css, "\\.seg[^{]* button\\[aria-pressed='true'\\]", 'background: ?var\\(--es-accent\\)');
	rule(css, "input\\[type='number'\\]", 'width: ?62px');
	assert.deepEqual(css.match(/#[0-9a-f]{3,8}\b|rgba?\(/gi) ?? [], []);
	const left = renderWith(ssr.EdgeCompatPop, { edge: 'L', onclose: () => undefined }).body;
	assert.match(left, /style="top: 8px; left: 486px;"/);
});

test('the top tab no longer mounts the library compatible panel', () => {
	/** [if] an edge renders [then] its caret is closed and no library panel or pop is in the markup, [else stop]. */
	const { body } = renderWith(ssr.EdgeWave, {
		edge: 'R',
		top: 28,
		height: 800,
		params: ssr.DEFAULT_WAVE,
		enabled: true,
		reduceMotion: true
	});
	assert.match(body, /aria-label="Compatible settings"[^>]*aria-expanded="false"/);
	assert.doesNotMatch(body, /compat-filter-panel|Compatible with master/);
});

test('the deck drag chip reads like the mock: deck loop, bars and the drag tag', () => {
	/** [if] deck 2 has a 16 beat loop engaged in the desktop shell [then] the chip reads "Deck 2 loop . 4 bars" then the tag, [else stop]. */
	const had = { window: globalThis.window, shell: globalThis.opendjShell };
	globalThis.window = globalThis;
	globalThis.opendjShell = { kind: 'electron' };
	try {
		const deck = { loop: { engaged: true, beat_length: 16 } };
		const { body, css } = renderWith(ssr.DeckLoopDragHandle, { deck, deckId: 2 });
		assert.match(body, /class="what[^"]*">Deck 2 loop \. 4 bars<\/span>/);
		assert.match(body, /<\/span>\s*<span class="tag[^"]*">drag to an edge<\/span>/);
		rule(css, '\\.loop-drag', 'gap: ?8px');
		rule(css, '\\.loop-drag', 'padding: ?6px 10px');
		rule(css, '\\.loop-drag', 'border-radius: ?5px');
		rule(css, '\\.loop-drag', 'font: ?12.5px var\\(--es-font-body\\)');
		rule(css, '\\.tag', 'font: ?12px var\\(--es-font-display\\)');
		rule(css, '\\.tag', 'text-transform: ?uppercase');
		rule(css, '\\.tag', 'color: ?var\\(--es-dim\\)');
		rule(css, '\\.loop-drag[^{]* svg', 'width: ?15px');
	} finally {
		globalThis.window = had.window;
		globalThis.opendjShell = had.shell;
	}
});

test('the sampler keyboard ring is the mock --warn', () => {
	/** [if] the sampler token sheet loads [then] the overlay, toggle and drag chip focus rings take --es-warn, [else stop]. */
	const css = readFileSync(
		new URL('../../src/lib/components/rb/sampler/edge-sampler-tokens.css', import.meta.url),
		'utf8'
	);
	const block = css.slice(css.indexOf('.edge-sampler :focus-visible'));
	assert.match(
		block,
		/^\.edge-sampler :focus-visible,\s*\.sampler-toggle:focus-visible,\s*\.loop-drag:focus-visible\s*\{/
	);
	assert.match(block, /\{\s*outline-color: var\(--es-warn\);\s*\}/);
});

test('the CUE-only deck wave fills to the mock .44, a tab wave to .46', () => {
	/** [if] one full-scale column is sized [then] a tab wave reaches 0.92 of the half height and the CUE deck 0.88, [else stop]. */
	const peaks = { low: [1], mid: [1], high: [1] };
	assert.equal(ssr.draw.regionColumnHeights(peaks, 1).low[0], 0.92);
	assert.equal(ssr.draw.CUE_DECK_WAVE_FILL, 0.88);
	assert.equal(ssr.draw.regionColumnHeights(peaks, 1, ssr.draw.CUE_DECK_WAVE_FILL).low[0], 0.88);
});
