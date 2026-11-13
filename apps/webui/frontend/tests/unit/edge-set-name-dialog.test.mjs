// requirement: EDGESAMP-14346
/**
 * Issue #10785 (S7): the set name dialog opened by a right-click on the top tab (mock
 * `openSetModal`, spec 7.4).
 *
 * Regression lines:
 * - if the set name dialog's elements, labels, limits or tokens drift from the mock then it stops looking like it
 * - if the dialog sits over the top tab instead of beside it then it hides the set it names
 * - if the top tab loses its right-click tip then nobody finds how to name a set
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

let ssr;
before(async () => {
	ssr = await loadSvelteSsrModule(
		[
			"export { default as EdgeWave } from '$lib/components/rb/sampler/EdgeWave.svelte';",
			"export { default as SetNameDialog } from '$lib/components/rb/sampler/SetNameDialog.svelte';",
			"export { DEFAULT_WAVE, HEAD_OPEN_W } from '$lib/player/sampler/edge-wave';",
			"export { render } from 'svelte/server';"
		].join('\n'),
		{ css: 'injected' }
	);
});

/** Server-render one component; returns its markup and all scoped CSS. */
function renderWith(component, props) {
	const { head, body } = ssr.render(component, { props });
	const css = [...head.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
	return { body, css };
}

const SET = { id: 'set-1', name: 'Warmup', description: 'Rolling drums', items: [] };

test('the set name dialog carries the mock elements, labels, limits and tokens', () => {
	/** [if] the R dialog renders for set Warmup [then] it has the mock's title, fields, limits, actions and --es- tokens, [else stop]. */
	const { body, css } = renderWith(ssr.SetNameDialog, {
		edge: 'R',
		set: SET,
		onclose: () => undefined
	});
	const order = [
		/role="dialog"[^>]*aria-label="Name this set"/,
		/<h3[^>]*>Name this set<\/h3>/,
		/<label class="lbl[^"]*" for="sampler-set-name-R">Name<\/label>/,
		/<input[^>]*maxlength="40"/,
		/<label class="lbl[^"]*" for="sampler-set-desc-R">Description<\/label>/,
		/<textarea[^>]*maxlength="200"/,
		/class="cancel[^"]*"[^>]*>Cancel</,
		/class="go[^"]*"[^>]*>Save</,
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
	assert.match(body, /value="Warmup"/);
	assert.match(body, />Rolling drums<\/textarea>/);
	const rule = (sel, decl) =>
		assert.match(css, new RegExp(`${sel}[^{]*\\{[^}]*${decl}`), `${sel} { ${decl} }`);
	rule('\\.pop', 'width: ?330px');
	rule('\\.pop', 'background: ?var\\(--es-panel\\)');
	rule('\\.pop', 'border: ?1px solid var\\(--es-border-strong\\)');
	rule('\\.pop', 'border-radius: ?7px');
	rule('\\.pop', 'box-shadow: ?0 14px 44px var\\(--es-shadow-pop\\)');
	rule('h3', 'font: ?18px var\\(--es-font-display\\)');
	rule('textarea', 'min-height: ?64px');
	rule('\\.acts[^{]*button\\.go', 'color: ?var\\(--es-accent-ink\\)');
	rule('\\.sum', 'font: ?12px var\\(--es-font-mono\\)');
	assert.deepEqual(css.match(/#[0-9a-f]{3,8}\b|rgba?\(/gi) ?? [], []);
	assert.deepEqual(css.match(/var\(--rb-[\w-]+\)/g) ?? [], []);
});

test('the dialog opens beside the top tab, never over it', () => {
	/** [if] the dialog renders on R then L [then] it sits 476 + 10 px in from that edge, 8 px down, [else stop]. */
	assert.equal(ssr.HEAD_OPEN_W, 476);
	const r = renderWith(ssr.SetNameDialog, { edge: 'R', set: SET, onclose: () => undefined }).body;
	assert.match(r, /style="top: 8px; right: 486px;"/);
	const l = renderWith(ssr.SetNameDialog, { edge: 'L', set: SET, onclose: () => undefined }).body;
	assert.match(l, /style="top: 8px; left: 486px;"/);
});

test('a right-click on the top tab is what names the set', () => {
	/** [if] an edge renders [then] its top tab is a labelled group whose idle tip says right-click names the set, [else stop]. */
	const { body } = renderWith(ssr.EdgeWave, {
		edge: 'R',
		top: 28,
		height: 800,
		params: ssr.DEFAULT_WAVE,
		enabled: true,
		reduceMotion: true
	});
	assert.match(
		body,
		/title="Right-click to name this set"[^>]*role="group" aria-label="Right edge sampler"/
	);
	assert.doesNotMatch(body, /role="dialog"/, 'closed until the right-click');
});
